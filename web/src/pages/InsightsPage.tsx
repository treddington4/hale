import { useMemo, useState } from "react"
import type { ChartConfiguration } from "chart.js"
import { useRuns, useAllRuns } from "@/hooks/useRuns"
import { useWellness, useMetrics } from "@/hooks/useWellness"
import { useSteps } from "@/hooks/useSteps"
import { useSleepStages } from "@/hooks/useSleepStages"
import { useHrFloor, isPlausibleHR } from "@/hooks/useHrFloor"
import { isPlausiblePace, paceStr } from "@/lib/format"
import { isRunActivity } from "@/lib/runs"
import { currentFilterRange, toDateInputValue, todayMidnight, addDays, type FilterMode } from "@/lib/dates"
import { FilterBar, type FilterState } from "@/components/activities/FilterBar"
import { ChartPanel } from "@/components/insights/ChartPanel"
import { ChartCanvas } from "@/components/insights/ChartCanvas"
import { CHART_COLORS, CHART_PAN_ZOOM } from "@/lib/chartTheme"
import {
  SLEEP_STAGE_ROWS,
  SLEEP_STAGE_KEY_TO_ROW,
  SLEEP_STAGE_COLORS,
  parseUtcTimestamp,
  fmtEstClock,
  estHourTicks,
} from "@/lib/sleepStages"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

const ROLLING_WINDOW_DAYS = 7

function buildTempScatter(
  points: { x: number; y: number }[],
  label: string,
  color: string,
  unit: string,
  tickFmt?: (v: number) => string,
  reverse = false,
): ChartConfiguration<"scatter"> {
  return {
    type: "scatter",
    data: { datasets: [{ label, data: points, backgroundColor: color }] },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const y = ctx.parsed.y as number
              return `${tickFmt ? tickFmt(y) : y + unit} at ${ctx.parsed.x}°F`
            },
          },
        },
      },
      scales: {
        x: { title: { display: true, text: "Temp (°F)", font: { size: 10 } }, grid: { color: CHART_COLORS.grid } },
        y: {
          reverse,
          ticks: tickFmt ? { callback: (v) => tickFmt(Number(v)) } : undefined,
          title: { display: true, text: label, font: { size: 10 } },
          grid: { color: CHART_COLORS.grid },
        },
      },
    },
  }
}

export function InsightsPage() {
  const [filter, setFilter] = useState<FilterState>(() => {
    const today = todayMidnight()
    return {
      mode: "rolling7" as FilterMode,
      anchor: today,
      customStart: addDays(today, -29),
      customEnd: today,
      activityType: "all",
    }
  })
  const [selectedNight, setSelectedNight] = useState<string | undefined>(undefined)

  const { start, end } = currentFilterRange(filter.mode, filter.anchor, filter.customStart, filter.customEnd)
  // Shared by every filter-driven chart on this page — previously only
  // runsQuery (and the charts built on it: Temperature's Effect, Weekly
  // Mileage, Pace/Cadence/HR Trend, Cadence vs Pace) actually responded to the
  // FilterBar; Daily Steps/Resting HR/VO2 Max/Sleep/Training Load silently kept
  // a fixed trailing window regardless of what the user picked (a real,
  // user-reported gap).
  const rangeQuery = filter.mode === "all"
    ? { all: true }
    : { start: toDateInputValue(start), end: toDateInputValue(end) }

  // Drag/swipe-to-scroll (ChartCanvas's onWindowDrag) shifts the FilterBar's
  // anchor by a number of days proportional to how far the chart was dragged,
  // then rangeQuery above (and every chart on this page) recomputes off the
  // new anchor — same mechanism as FilterBar's own prev/next buttons, just
  // continuous instead of a fixed 7-day step. Only meaningful for modes with a
  // fixed-length window; ytd (grows from Jan 1), custom (has explicit date
  // pickers), and all (the entire history) have nothing well-defined to shift.
  const SHIFTABLE_MODES: FilterMode[] = ["rolling7", "week", "month", "sixMonths", "year"]
  const dragEnabled = SHIFTABLE_MODES.includes(filter.mode)
  const handleWindowDrag = (fraction: number) => {
    if (!dragEnabled) return
    const windowDays = Math.round((end.getTime() - start.getTime()) / 86400000) + 1
    const shiftDays = Math.round(-fraction * windowDays)
    if (shiftDays === 0) return
    const today = todayMidnight()
    const nextAnchor = addDays(filter.anchor, shiftDays)
    setFilter((f) => ({ ...f, anchor: nextAnchor > today ? today : nextAnchor }))
  }
  const runsQuery = useRuns(rangeQuery)
  const allRunsQuery = useAllRuns()
  const metricsQuery = useMetrics(rangeQuery)
  const wellnessQuery = useWellness(rangeQuery)
  const stepsQuery = useSteps(rangeQuery)
  const sleepStagesQuery = useSleepStages(selectedNight)
  const hrFloor = useHrFloor()

  const activityTypeCounts = useMemo(() => {
    if (!runsQuery.data) return []
    const counts: Record<string, number> = {}
    runsQuery.data.forEach((r) => {
      const t = r.activityType || "Run"
      counts[t] = (counts[t] || 0) + 1
    })
    return Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([type, count]) => ({ type, count }))
  }, [runsQuery.data])

  // Insights is entirely running-specific (pace/cadence/HR trends) — other captured
  // activity types shouldn't skew these, so this always intersects with isRunActivity
  // on top of whatever the (shared) filter bar's activityType select produced.
  const data = useMemo(() => {
    if (!runsQuery.data) return []
    const byType =
      filter.activityType === "all"
        ? runsQuery.data
        : runsQuery.data.filter((r) => (r.activityType || "Run") === filter.activityType)
    return byType.filter(isRunActivity)
  }, [runsQuery.data, filter.activityType])

  const outdoor = useMemo(() => data.filter((r) => !r.isTreadmill && r.tempF != null), [data])
  const perfData = useMemo(
    () => [...data].sort((a, b) => (a.date < b.date ? -1 : 1)).filter((r) => isPlausiblePace(r.avgPaceSecPerMi, r.distanceMi)),
    [data],
  )
  const cadPaceData = useMemo(
    () => data.filter((r) => r.avgCadence && isPlausiblePace(r.avgPaceSecPerMi, r.distanceMi)),
    [data],
  )

  // Rolling window looks back across ALL running history, not just the currently
  // filtered range — otherwise the first few points of a filtered view (e.g. "last
  // 30 days") would be computed from an artificially thin lookback window.
  const allRunHistory = useMemo(() => (allRunsQuery.data ?? []).filter(isRunActivity), [allRunsQuery.data])
  const rollingPaceData = useMemo(() => {
    return perfData
      .map((r) => {
        const rEnd = new Date(r.date + "T00:00:00")
        const rStart = new Date(rEnd)
        rStart.setDate(rStart.getDate() - (ROLLING_WINDOW_DAYS - 1))
        const windowRuns = allRunHistory.filter((rr) => {
          const d = new Date(rr.date + "T00:00:00")
          return d >= rStart && d <= rEnd && isPlausiblePace(rr.avgPaceSecPerMi, rr.distanceMi)
        })
        const totalDist = windowRuns.reduce((s, rr) => s + (rr.distanceMi || 0), 0)
        const pace = totalDist
          ? windowRuns.reduce((s, rr) => s + (rr.avgPaceSecPerMi || 0) * (rr.distanceMi || 0), 0) / totalDist
          : null
        return { date: r.date, pace }
      })
      .filter((p): p is { date: string; pace: number } => p.pace != null)
  }, [perfData, allRunHistory])

  const weeklyEntries = useMemo(() => {
    const weekly: Record<string, number> = {}
    data.forEach((r) => {
      const d = new Date(r.date)
      const monday = new Date(d)
      monday.setDate(d.getDate() - ((d.getDay() + 6) % 7))
      const key = monday.toISOString().slice(0, 10)
      weekly[key] = (weekly[key] || 0) + (r.distanceMi || 0)
    })
    return Object.entries(weekly).sort(([a], [b]) => (a < b ? -1 : 1))
  }, [data])

  const wellness = wellnessQuery.data
  const rhrData = useMemo(() => (wellness ?? []).filter((d) => d.restingHrBpm != null), [wellness])
  const vo2Data = useMemo(() => (wellness ?? []).filter((d) => d.vo2max != null), [wellness])
  const sleepData = useMemo(
    () => (wellness ?? []).filter((d) => d.sleepScore != null || d.sleepSeconds != null),
    [wellness],
  )
  const stepsData = useMemo(() => stepsQuery.data ?? [], [stepsQuery.data])

  const tempHrConfig = useMemo(() => {
    const pts = outdoor
      .map((r) => ({ x: r.tempF as number, y: r.avgHR as number }))
      .filter((p) => isPlausibleHR(p.y, hrFloor))
    return pts.length ? buildTempScatter(pts, "Avg HR", CHART_COLORS.orange, "bpm") : null
  }, [outdoor, hrFloor])

  const tempPaceConfig = useMemo(() => {
    const pts = outdoor
      .map((r) => ({ x: r.tempF as number, y: r.avgPaceSecPerMi as number, distanceMi: r.distanceMi }))
      .filter((p) => isPlausiblePace(p.y, p.distanceMi))
    return pts.length ? buildTempScatter(pts, "Pace", CHART_COLORS.gold, "", (v) => paceStr(v) + "/mi", true) : null
  }, [outdoor])

  const tempCadConfig = useMemo(() => {
    const pts = outdoor.map((r) => ({ x: r.tempF as number, y: r.avgCadence as number })).filter((p) => p.y != null)
    return pts.length ? buildTempScatter(pts, "Cadence", CHART_COLORS.cyan, "spm") : null
  }, [outdoor])

  // Phase 6.2 — PMC (fitness/fatigue/form). CTL/ATL as lines (their own axis,
  // since they're unbounded rolling averages of daily load); TSB as a bar on a
  // second axis, colored per-bar (green = fresh/positive, orange = fatigued/
  // negative) since that sign is the whole point of "form."
  const pmcConfig = useMemo<ChartConfiguration | null>(() => {
    const points = metricsQuery.data
    if (!points || points.length < 2) return null
    return {
      type: "line",
      data: {
        labels: points.map((p) => p.date.slice(5)),
        datasets: [
          {
            type: "bar",
            label: "Form (TSB)",
            data: points.map((p) => p.tsb),
            backgroundColor: points.map((p) => (p.tsb >= 0 ? CHART_COLORS.green : CHART_COLORS.orange)),
            yAxisID: "tsb",
            order: 2,
          },
          {
            type: "line",
            label: "Fitness (CTL)",
            data: points.map((p) => p.ctl),
            borderColor: CHART_COLORS.gold,
            backgroundColor: CHART_COLORS.gold,
            yAxisID: "load",
            tension: 0.25,
            pointRadius: 0,
            order: 0,
          },
          {
            type: "line",
            label: "Fatigue (ATL)",
            data: points.map((p) => p.atl),
            borderColor: CHART_COLORS.cyan,
            backgroundColor: CHART_COLORS.cyan,
            yAxisID: "load",
            tension: 0.25,
            pointRadius: 0,
            order: 1,
          },
        ],
      },
      options: {
        plugins: {
          legend: { display: true, position: "top", labels: { boxHeight: 8 } },
          zoom: CHART_PAN_ZOOM,
        },
        scales: {
          load: { type: "linear", position: "left", grid: { color: CHART_COLORS.grid } },
          tsb: { type: "linear", position: "right", grid: { display: false } },
          x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
        },
      },
    }
  }, [metricsQuery.data])

  const mileageConfig = useMemo<ChartConfiguration<"bar"> | null>(() => {
    if (!weeklyEntries.length) return null
    return {
      type: "bar",
      data: {
        labels: weeklyEntries.map(([w]) => w.slice(5)),
        datasets: [{ data: weeklyEntries.map(([, mi]) => +mi.toFixed(1)), backgroundColor: CHART_COLORS.cyan, borderRadius: 4 }],
      },
      options: {
        plugins: { legend: { display: false }, zoom: CHART_PAN_ZOOM },
        scales: { x: { grid: { display: false } }, y: { grid: { color: CHART_COLORS.grid } } },
      },
    }
  }, [weeklyEntries])

  const perfConfig = useMemo<ChartConfiguration<"line"> | null>(() => {
    if (perfData.length < 2) return null
    return {
      type: "line",
      data: {
        labels: perfData.map((r) => r.date.slice(5)),
        datasets: [
          {
            label: "Pace",
            data: perfData.map((r) => (r.avgPaceSecPerMi as number) / 60),
            borderColor: CHART_COLORS.gold,
            backgroundColor: CHART_COLORS.gold,
            yAxisID: "pace",
            tension: 0.3,
          },
          {
            label: "Cadence",
            data: perfData.map((r) => r.avgCadence ?? null),
            borderColor: CHART_COLORS.cyan,
            backgroundColor: CHART_COLORS.cyan,
            yAxisID: "bpm",
            tension: 0.3,
            spanGaps: true,
          },
          {
            label: "Avg HR",
            data: perfData.map((r) => (isPlausibleHR(r.avgHR, hrFloor) ? r.avgHR : null)),
            borderColor: CHART_COLORS.orange,
            backgroundColor: CHART_COLORS.orange,
            yAxisID: "bpm",
            tension: 0.3,
            spanGaps: true,
          },
        ],
      },
      options: {
        plugins: { legend: { display: false }, zoom: CHART_PAN_ZOOM },
        scales: {
          pace: {
            type: "linear",
            position: "left",
            reverse: true,
            ticks: { callback: (v) => paceStr(Number(v) * 60) },
            grid: { color: CHART_COLORS.grid },
          },
          bpm: { type: "linear", position: "right", grid: { display: false } },
          x: { grid: { display: false } },
        },
      },
    }
  }, [perfData, hrFloor])

  const rollingPaceConfig = useMemo<ChartConfiguration<"line"> | null>(() => {
    if (rollingPaceData.length < 2) return null
    return {
      type: "line",
      data: {
        labels: rollingPaceData.map((p) => p.date.slice(5)),
        datasets: [
          {
            label: "Rolling avg pace",
            data: rollingPaceData.map((p) => p.pace / 60),
            borderColor: CHART_COLORS.gold,
            backgroundColor: CHART_COLORS.gold,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => paceStr((ctx.parsed.y as number) * 60) + "/mi" } },
          zoom: CHART_PAN_ZOOM,
        },
        scales: {
          y: { reverse: true, ticks: { callback: (v) => paceStr(Number(v) * 60) }, grid: { color: CHART_COLORS.grid } },
          x: { grid: { display: false } },
        },
      },
    }
  }, [rollingPaceData])

  const cadPaceConfig = useMemo<ChartConfiguration<"scatter"> | null>(() => {
    if (cadPaceData.length < 2) return null
    return {
      type: "scatter",
      data: {
        datasets: [
          {
            data: cadPaceData.map((r) => ({ x: (r.avgPaceSecPerMi as number) / 60, y: r.avgCadence as number })),
            backgroundColor: CHART_COLORS.cyan,
          },
        ],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: {
            reverse: true,
            ticks: { callback: (v) => paceStr(Number(v) * 60) },
            title: { display: true, text: "Pace" },
            grid: { color: CHART_COLORS.grid },
          },
          y: { title: { display: true, text: "Cadence (spm)" }, grid: { color: CHART_COLORS.grid } },
        },
      },
    }
  }, [cadPaceData])

  const stepsConfig = useMemo<ChartConfiguration<"bar"> | null>(() => {
    if (stepsData.length < 2) return null
    return {
      type: "bar",
      data: {
        labels: stepsData.map((d) => d.date.slice(5)),
        datasets: [{ data: stepsData.map((d) => d.steps), backgroundColor: CHART_COLORS.green, borderRadius: 4 }],
      },
      options: {
        plugins: { legend: { display: false }, zoom: CHART_PAN_ZOOM },
        scales: { x: { grid: { display: false } }, y: { grid: { color: CHART_COLORS.grid } } },
      },
    }
  }, [stepsData])

  const rhrConfig = useMemo<ChartConfiguration<"line"> | null>(() => {
    if (rhrData.length < 2) return null
    return {
      type: "line",
      data: {
        labels: rhrData.map((d) => d.date.slice(5)),
        datasets: [{ data: rhrData.map((d) => d.restingHrBpm), borderColor: CHART_COLORS.orange, backgroundColor: CHART_COLORS.orange, tension: 0.3 }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => `${ctx.parsed.y} bpm` } },
          zoom: CHART_PAN_ZOOM,
        },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: (v) => v + " bpm" }, grid: { color: CHART_COLORS.grid } },
        },
      },
    }
  }, [rhrData])

  const vo2Config = useMemo<ChartConfiguration<"line"> | null>(() => {
    if (vo2Data.length < 2) return null
    return {
      type: "line",
      data: {
        labels: vo2Data.map((d) => d.date.slice(5)),
        datasets: [{ data: vo2Data.map((d) => d.vo2max), borderColor: CHART_COLORS.gold, backgroundColor: CHART_COLORS.gold, tension: 0.3, stepped: true }],
      },
      options: {
        plugins: { legend: { display: false }, zoom: CHART_PAN_ZOOM },
        scales: { x: { grid: { display: false } }, y: { grid: { color: CHART_COLORS.grid } } },
      },
    }
  }, [vo2Data])

  const sleepConfig = useMemo<ChartConfiguration<"line"> | null>(() => {
    if (sleepData.length < 2) return null
    return {
      type: "line",
      data: {
        labels: sleepData.map((d) => d.date.slice(5)),
        datasets: [
          {
            label: "Sleep score",
            data: sleepData.map((d) => d.sleepScore),
            borderColor: CHART_COLORS.green,
            backgroundColor: CHART_COLORS.green,
            yAxisID: "score",
            tension: 0.3,
            spanGaps: true,
          },
          {
            label: "Duration (hrs)",
            data: sleepData.map((d) => (d.sleepSeconds ? +(d.sleepSeconds / 3600).toFixed(1) : null)),
            borderColor: CHART_COLORS.cyan,
            backgroundColor: CHART_COLORS.cyan,
            yAxisID: "hrs",
            tension: 0.3,
            spanGaps: true,
          },
        ],
      },
      options: {
        plugins: { legend: { display: true, labels: { boxWidth: 10 } }, zoom: CHART_PAN_ZOOM },
        scales: {
          score: { type: "linear", position: "left", min: 0, max: 100, grid: { color: CHART_COLORS.grid } },
          hrs: { type: "linear", position: "right", min: 0, grid: { display: false } },
          x: { grid: { display: false } },
        },
      },
    }
  }, [sleepData])

  const hypnogramConfig = useMemo<ChartConfiguration<"bar"> | null>(() => {
    const segments = sleepStagesQuery.data?.segments ?? []
    if (!segments.length) return null
    const points = segments.map((s) => ({
      y: SLEEP_STAGE_KEY_TO_ROW[s.stage] || s.stage,
      x: [parseUtcTimestamp(s.start), parseUtcTimestamp(s.end)] as [number, number],
    }))
    const minMs = Math.min(...points.map((p) => p.x[0]))
    const maxMs = Math.max(...points.map((p) => p.x[1]))
    return {
      type: "bar",
      data: {
        // Chart.js's floating-bar-on-a-category-y-axis pattern (x: [start,end],
        // y: label) is a real, documented feature, but its bundled TS types don't
        // model this data shape (they expect Point/BubbleDataPoint) — hence the cast.
        datasets: [
          {
            data: points as unknown as (number | null)[],
            backgroundColor: points.map((p) => SLEEP_STAGE_COLORS[p.y] || CHART_COLORS.muted),
            barPercentage: 1,
            categoryPercentage: 0.9,
          },
        ],
      },
      options: {
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => (items[0].raw as { y: string }).y,
              label: (ctx) => {
                const raw = ctx.raw as { x: [number, number] }
                return `${fmtEstClock(raw.x[0])} – ${fmtEstClock(raw.x[1])} (${Math.round((raw.x[1] - raw.x[0]) / 60000)} min)`
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: minMs,
            max: maxMs,
            afterBuildTicks: (scale) => {
              scale.ticks = estHourTicks(scale.min, scale.max).map((v) => ({ value: v }))
            },
            ticks: { callback: (value) => fmtEstClock(Number(value)) },
            title: { display: true, text: "Time (EST)" },
            grid: { color: CHART_COLORS.grid },
          },
          y: { type: "category", labels: SLEEP_STAGE_ROWS, grid: { display: false } },
        },
      },
    }
  }, [sleepStagesQuery.data])

  if (!runsQuery.data) return <Skeleton className="h-64 w-full" />

  const noRunsInRange = data.length === 0 && runsQuery.data.length > 0

  return (
    <div className="flex flex-col gap-4">
      <ChartPanel
        title="Training Load (Fitness / Fatigue / Form)"
        sub="CTL (42d fitness) & ATL (7d fatigue) vs. threshold-HR-based training load; TSB (form) — positive means fresh, negative means fatigued"
        empty={!pmcConfig ? "Needs at least a couple of days of real training load (Phase 6.1's per-run TSS) to plot." : null}
      >
        {pmcConfig && (
          <ChartCanvas
            config={pmcConfig}
            height={220}
            onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
            loading={metricsQuery.isFetching}
          />
        )}
      </ChartPanel>

      <FilterBar state={filter} onChange={setFilter} activityTypeCounts={activityTypeCounts} />

      {noRunsInRange ? (
        <div className="text-hale-faint flex h-24 items-center justify-center text-xs">No runs in this range.</div>
      ) : (
        <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
          <ChartPanel
            title="Temperature's Effect"
            sub="Pace, cadence, and HR vs. outdoor temp"
            empty={outdoor.length < 2 ? "Log a few more outdoor runs across different temps." : null}
          >
            {tempHrConfig && <ChartCanvas config={tempHrConfig} height={120} />}
            {tempPaceConfig && <ChartCanvas config={tempPaceConfig} height={120} />}
            {tempCadConfig && <ChartCanvas config={tempCadConfig} height={120} />}
          </ChartPanel>

          <ChartPanel title="Weekly Mileage" empty={!mileageConfig ? "Not enough data yet." : null}>
            {mileageConfig && (
              <ChartCanvas
                config={mileageConfig}
                height={160}
                onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                loading={runsQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Pace, Cadence & HR Trend"
            sub="How your speed, turnover, and cardiac cost move together over time"
            empty={!perfConfig ? "Need a couple more runs." : null}
          >
            {perfConfig && (
              <>
                <ChartCanvas
                  config={perfConfig}
                  height={200}
                  onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                  loading={runsQuery.isFetching}
                />
                <div className="text-muted-foreground mt-2 flex gap-4 text-xs">
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block size-2 rounded-full" style={{ background: CHART_COLORS.gold }} />
                    Pace (left axis)
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block size-2 rounded-full" style={{ background: CHART_COLORS.cyan }} />
                    Cadence
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block size-2 rounded-full" style={{ background: CHART_COLORS.orange }} />
                    Avg HR
                  </span>
                </div>
              </>
            )}
          </ChartPanel>

          <ChartPanel
            title={`Average Pace (${ROLLING_WINDOW_DAYS}-Day Rolling)`}
            sub="Distance-weighted average pace over the trailing week, smoothing out day-to-day noise"
            empty={!rollingPaceConfig ? "Need a couple more runs." : null}
          >
            {rollingPaceConfig && (
              <ChartCanvas
                config={rollingPaceConfig}
                height={160}
                onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                loading={runsQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Cadence vs. Pace"
            sub="Are you turning your legs over faster as pace increases, or overstriding?"
            empty={!cadPaceConfig ? "Need a couple more runs." : null}
          >
            {cadPaceConfig && <ChartCanvas config={cadPaceConfig} height={200} />}
          </ChartPanel>

          <ChartPanel
            title="Daily Steps"
            sub="Garmin wellness data"
            empty={!stepsConfig ? "No step data synced yet (Garmin-only)." : null}
          >
            {stepsConfig && (
              <ChartCanvas
                config={stepsConfig}
                height={140}
                onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                loading={stepsQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Resting Heart Rate"
            sub="Garmin wellness data"
            empty={!rhrConfig ? "No resting HR data synced yet (Garmin-only)." : null}
          >
            {rhrConfig && (
              <ChartCanvas
                config={rhrConfig}
                height={140}
                onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                loading={wellnessQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="VO2 Max"
            sub="Garmin wellness data — updates periodically, not every day"
            empty={!vo2Config ? "No VO2 max data synced yet (Garmin-only)." : null}
          >
            {vo2Config && (
              <ChartCanvas
                config={vo2Config}
                height={140}
                onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                loading={wellnessQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Sleep"
            sub="Sleep score and total duration"
            empty={!sleepConfig ? "No sleep data synced yet (Garmin-only)." : null}
          >
            {sleepConfig && (
              <ChartCanvas
                config={sleepConfig}
                height={160}
                onWindowDrag={dragEnabled ? handleWindowDrag : undefined}
                loading={wellnessQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Sleep Stages"
            sub="What stage you were in, minute by minute, for one night"
            empty={!sleepStagesQuery.data?.availableDates.length ? "No sleep stage data synced yet (Garmin-only)." : null}
            action={
              sleepStagesQuery.data?.availableDates.length ? (
                <Select
                  value={selectedNight ?? sleepStagesQuery.data.date ?? undefined}
                  onValueChange={setSelectedNight}
                >
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {[...sleepStagesQuery.data.availableDates].reverse().map((d) => (
                      <SelectItem key={d} value={d}>
                        {d}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : undefined
            }
          >
            {hypnogramConfig && <ChartCanvas config={hypnogramConfig} height={140} />}
          </ChartPanel>
        </div>
      )}
    </div>
  )
}
