import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import type { ChartConfiguration } from "chart.js"
import { useRuns, useAllRuns } from "@/hooks/useRuns"
import { useWellness, useMetrics } from "@/hooks/useWellness"
import { useSteps } from "@/hooks/useSteps"
import { useSleepStages } from "@/hooks/useSleepStages"
import { useHrFloor, isPlausibleHR } from "@/hooks/useHrFloor"
import { isPlausiblePace, paceStr } from "@/lib/format"
import { isRunActivity, type Run } from "@/lib/runs"
import { currentFilterRange, toDateInputValue, todayMidnight, addDays, type FilterMode } from "@/lib/dates"
import { FilterBar, type FilterState } from "@/components/activities/FilterBar"
import type { RunsQuery, WellnessDay, DailyStepsPoint, DailyMetricPoint } from "@/lib/api"
import { ChartPanel } from "@/components/insights/ChartPanel"
import { ChartCanvas } from "@/components/insights/ChartCanvas"
import { ChartCarousel } from "@/components/insights/ChartCarousel"
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
// Modes with a fixed-length window that can be meaningfully paged through by
// swiping a chart or the FilterBar's prev/next buttons. ytd (grows from Jan
// 1), custom (has explicit date pickers), and all (entire history) have no
// well-defined "one window" to page by.
const SHIFTABLE_MODES: FilterMode[] = ["rolling7", "week", "month", "sixMonths", "year"]

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

// ---- pure derivation helpers (called once per page: prev/current/next) ----

function deriveRunChartData(runs: Run[] | undefined, activityType: string) {
  const source = runs ?? []
  const byType = activityType === "all" ? source : source.filter((r) => (r.activityType || "Run") === activityType)
  const data = byType.filter(isRunActivity)
  const outdoor = data.filter((r) => !r.isTreadmill && r.tempF != null)
  const perfData = [...data]
    .sort((a, b) => (a.date < b.date ? -1 : 1))
    .filter((r) => isPlausiblePace(r.avgPaceSecPerMi, r.distanceMi))
  const cadPaceData = data.filter((r) => r.avgCadence && isPlausiblePace(r.avgPaceSecPerMi, r.distanceMi))
  const weekly: Record<string, number> = {}
  data.forEach((r) => {
    const d = new Date(r.date)
    const monday = new Date(d)
    monday.setDate(d.getDate() - ((d.getDay() + 6) % 7))
    const key = monday.toISOString().slice(0, 10)
    weekly[key] = (weekly[key] || 0) + (r.distanceMi || 0)
  })
  const weeklyEntries = Object.entries(weekly).sort(([a], [b]) => (a < b ? -1 : 1))
  return { data, outdoor, perfData, cadPaceData, weeklyEntries }
}

// Rolling window looks back across ALL running history, not just the currently
// filtered page — otherwise the first few points of a filtered view would be
// computed from an artificially thin lookback window.
function deriveRollingPace(perfData: Run[], allRunHistory: Run[]) {
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
}

// ---- pure chart config builders (one page's worth of data in, one config out) ----

// Phase 6.2 — PMC (fitness/fatigue/form). CTL/ATL as lines (their own axis,
// since they're unbounded rolling averages of daily load); TSB as a bar on a
// second axis, colored per-bar (green = fresh/positive, orange = fatigued/
// negative) since that sign is the whole point of "form."
function buildPmcConfig(points: DailyMetricPoint[] | undefined): ChartConfiguration | null {
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
}

function buildMileageConfig(weeklyEntries: [string, number][]): ChartConfiguration<"bar"> | null {
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
}

function buildPerfConfig(perfData: Run[], hrFloor: number): ChartConfiguration<"line"> | null {
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
}

function buildRollingPaceConfig(rollingPaceData: { date: string; pace: number }[]): ChartConfiguration<"line"> | null {
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
}

function buildStepsConfig(stepsData: DailyStepsPoint[]): ChartConfiguration<"bar"> | null {
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
}

function buildRhrConfig(rhrData: WellnessDay[]): ChartConfiguration<"line"> | null {
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
}

function buildVo2Config(vo2Data: WellnessDay[]): ChartConfiguration<"line"> | null {
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
}

function buildSleepConfig(sleepData: WellnessDay[]): ChartConfiguration<"line"> | null {
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
}

// Runs a builder over one page's data, memoized per page independently — so
// re-rendering because e.g. only the "next" page's query resolved doesn't
// tear down and rebuild the already-stable "prev"/"current" Chart.js
// instances (see ChartCanvas's own comment on why callers must memoize).
function usePagedConfigs<T, C>(current: T, prev: T, next: T, builder: (v: T) => C | null) {
  const c = useMemo(() => builder(current), [current, builder])
  const p = useMemo(() => builder(prev), [prev, builder])
  const n = useMemo(() => builder(next), [next, builder])
  return { prev: p, current: c, next: n }
}

function useFiltered<T>(data: T[] | undefined, pred: (d: T) => boolean): T[] {
  return useMemo(() => (data ?? []).filter(pred), [data, pred])
}

function useOrEmpty<T>(data: T[] | undefined): T[] {
  return useMemo(() => data ?? [], [data])
}

// Stable (module-scope) predicates for useFiltered — a fresh inline closure
// every render would be technically safe here too (none of these capture a
// changing outer variable) but would still fail the exhaustive-deps lint and
// invite exactly the kind of stale-closure bug `perfBuilder` below guards
// against for real, so keep the pattern consistent.
const hasRestingHr = (d: WellnessDay) => d.restingHrBpm != null
const hasVo2Max = (d: WellnessDay) => d.vo2max != null
const hasSleepData = (d: WellnessDay) => d.sleepScore != null || d.sleepSeconds != null

export function InsightsPage() {
  const queryClient = useQueryClient()
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
  const windowDays = Math.round((end.getTime() - start.getTime()) / 86400000) + 1
  const today = todayMidnight()
  const dragEnabled = SHIFTABLE_MODES.includes(filter.mode)

  // Shared by every filter-driven chart on this page.
  const rangeQuery: RunsQuery = filter.mode === "all"
    ? { all: true }
    : { start: toDateInputValue(start), end: toDateInputValue(end) }

  // Swipe/drag-to-page (ChartCarousel) pre-fetches the adjacent window on
  // each side so dragging reveals real, already-rendered data instead of
  // blank canvas — see ChartCarousel.tsx. "next" is only fetched once the
  // current page doesn't already reach today (there's nothing real to page
  // forward into from "today"), which also means the very first load only
  // ever fetches the earlier/"low" neighbor, not both.
  const prevAnchor = addDays(filter.anchor, -windowDays)
  const nextAnchor = addDays(filter.anchor, windowDays)
  const hasNext = dragEnabled && end < today
  const prevRange = dragEnabled ? currentFilterRange(filter.mode, prevAnchor, filter.customStart, filter.customEnd) : null
  const nextRange = hasNext ? currentFilterRange(filter.mode, nextAnchor, filter.customStart, filter.customEnd) : null
  const prevRangeQuery: RunsQuery | null = prevRange ? { start: toDateInputValue(prevRange.start), end: toDateInputValue(prevRange.end) } : null
  const nextRangeQuery: RunsQuery | null = nextRange ? { start: toDateInputValue(nextRange.start), end: toDateInputValue(nextRange.end) } : null

  const handlePageShift = (direction: -1 | 1) => {
    const shifted = addDays(filter.anchor, direction * windowDays)
    setFilter((f) => ({ ...f, anchor: shifted > today ? today : shifted }))
  }

  const runsQuery = useRuns(rangeQuery)
  const prevRunsQuery = useRuns(prevRangeQuery ?? {}, !!prevRangeQuery)
  const nextRunsQuery = useRuns(nextRangeQuery ?? {}, !!nextRangeQuery)
  const allRunsQuery = useAllRuns()
  const metricsQuery = useMetrics(rangeQuery)
  const prevMetricsQuery = useMetrics(prevRangeQuery ?? {}, !!prevRangeQuery)
  const nextMetricsQuery = useMetrics(nextRangeQuery ?? {}, !!nextRangeQuery)
  const wellnessQuery = useWellness(rangeQuery)
  const prevWellnessQuery = useWellness(prevRangeQuery ?? {}, !!prevRangeQuery)
  const nextWellnessQuery = useWellness(nextRangeQuery ?? {}, !!nextRangeQuery)
  const stepsQuery = useSteps(rangeQuery)
  const prevStepsQuery = useSteps(prevRangeQuery ?? {}, !!prevRangeQuery)
  const nextStepsQuery = useSteps(nextRangeQuery ?? {}, !!nextRangeQuery)
  const sleepStagesQuery = useSleepStages(selectedNight)
  const hrFloor = useHrFloor()

  // Cache eviction: as the user pages back and forth, anything more than 2
  // pages away from the current one gets dropped from React Query's cache
  // (not just left to time-based gc) — otherwise a long back-and-forth
  // session would keep every visited window's 4 queries (runs/metrics/
  // wellness/steps) resident indefinitely.
  const visitedPagesRef = useRef<Map<string, RunsQuery>>(new Map())
  useEffect(() => {
    if (!dragEnabled) return
    const anchorKey = toDateInputValue(filter.anchor)
    visitedPagesRef.current.set(anchorKey, rangeQuery)
    const currentTime = filter.anchor.getTime()
    for (const [key, range] of visitedPagesRef.current) {
      const keyTime = new Date(key + "T00:00:00").getTime()
      const pageDistance = Math.round(Math.abs(keyTime - currentTime) / (windowDays * 86400000))
      if (pageDistance > 2) {
        queryClient.removeQueries({ queryKey: ["runs", range] })
        queryClient.removeQueries({ queryKey: ["metrics", range] })
        queryClient.removeQueries({ queryKey: ["wellness", range] })
        queryClient.removeQueries({ queryKey: ["steps", range] })
        visitedPagesRef.current.delete(key)
      }
    }
    // rangeQuery is derived fresh from filter.anchor/mode each render, not an
    // independent trigger — re-running this effect off filter.anchor alone
    // (plus dragEnabled/windowDays, which only change alongside mode) is
    // correct and avoids re-running on every unrelated re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter.anchor, dragEnabled, windowDays, queryClient])

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

  // Insights is entirely running-specific — intersected with isRunActivity on
  // top of the FilterBar's activityType select, same as before. Derived once
  // per page (current/prev/next) via the shared helper above.
  const currentRun = useMemo(() => deriveRunChartData(runsQuery.data, filter.activityType), [runsQuery.data, filter.activityType])
  const prevRun = useMemo(() => deriveRunChartData(prevRunsQuery.data, filter.activityType), [prevRunsQuery.data, filter.activityType])
  const nextRun = useMemo(() => deriveRunChartData(nextRunsQuery.data, filter.activityType), [nextRunsQuery.data, filter.activityType])

  const allRunHistory = useMemo(() => (allRunsQuery.data ?? []).filter(isRunActivity), [allRunsQuery.data])
  const currentRollingPace = useMemo(() => deriveRollingPace(currentRun.perfData, allRunHistory), [currentRun.perfData, allRunHistory])
  const prevRollingPace = useMemo(() => deriveRollingPace(prevRun.perfData, allRunHistory), [prevRun.perfData, allRunHistory])
  const nextRollingPace = useMemo(() => deriveRollingPace(nextRun.perfData, allRunHistory), [nextRun.perfData, allRunHistory])

  const currentRhr = useFiltered(wellnessQuery.data, hasRestingHr)
  const prevRhr = useFiltered(prevWellnessQuery.data, hasRestingHr)
  const nextRhr = useFiltered(nextWellnessQuery.data, hasRestingHr)
  const currentVo2 = useFiltered(wellnessQuery.data, hasVo2Max)
  const prevVo2 = useFiltered(prevWellnessQuery.data, hasVo2Max)
  const nextVo2 = useFiltered(nextWellnessQuery.data, hasVo2Max)
  const currentSleep = useFiltered(wellnessQuery.data, hasSleepData)
  const prevSleep = useFiltered(prevWellnessQuery.data, hasSleepData)
  const nextSleep = useFiltered(nextWellnessQuery.data, hasSleepData)

  const currentStepsData = useOrEmpty(stepsQuery.data)
  const prevStepsData = useOrEmpty(prevStepsQuery.data)
  const nextStepsData = useOrEmpty(nextStepsQuery.data)

  const tempHrConfig = useMemo(() => {
    const pts = currentRun.outdoor
      .map((r) => ({ x: r.tempF as number, y: r.avgHR as number }))
      .filter((p) => isPlausibleHR(p.y, hrFloor))
    return pts.length ? buildTempScatter(pts, "Avg HR", CHART_COLORS.orange, "bpm") : null
  }, [currentRun.outdoor, hrFloor])

  const tempPaceConfig = useMemo(() => {
    const pts = currentRun.outdoor
      .map((r) => ({ x: r.tempF as number, y: r.avgPaceSecPerMi as number, distanceMi: r.distanceMi }))
      .filter((p) => isPlausiblePace(p.y, p.distanceMi))
    return pts.length ? buildTempScatter(pts, "Pace", CHART_COLORS.gold, "", (v) => paceStr(v) + "/mi", true) : null
  }, [currentRun.outdoor])

  const tempCadConfig = useMemo(() => {
    const pts = currentRun.outdoor.map((r) => ({ x: r.tempF as number, y: r.avgCadence as number })).filter((p) => p.y != null)
    return pts.length ? buildTempScatter(pts, "Cadence", CHART_COLORS.cyan, "spm") : null
  }, [currentRun.outdoor])

  const cadPaceConfig = useMemo<ChartConfiguration<"scatter"> | null>(() => {
    if (currentRun.cadPaceData.length < 2) return null
    return {
      type: "scatter",
      data: {
        datasets: [
          {
            data: currentRun.cadPaceData.map((r) => ({ x: (r.avgPaceSecPerMi as number) / 60, y: r.avgCadence as number })),
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
  }, [currentRun.cadPaceData])

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

  const pmc = usePagedConfigs(metricsQuery.data, prevMetricsQuery.data, nextMetricsQuery.data, buildPmcConfig)
  const mileage = usePagedConfigs(currentRun.weeklyEntries, prevRun.weeklyEntries, nextRun.weeklyEntries, buildMileageConfig)
  const perfBuilder = useCallback((d: Run[]) => buildPerfConfig(d, hrFloor), [hrFloor])
  const perf = usePagedConfigs(currentRun.perfData, prevRun.perfData, nextRun.perfData, perfBuilder)
  const rollingPace = usePagedConfigs(currentRollingPace, prevRollingPace, nextRollingPace, buildRollingPaceConfig)
  const steps = usePagedConfigs(currentStepsData, prevStepsData, nextStepsData, buildStepsConfig)
  const rhr = usePagedConfigs(currentRhr, prevRhr, nextRhr, buildRhrConfig)
  const vo2 = usePagedConfigs(currentVo2, prevVo2, nextVo2, buildVo2Config)
  const sleep = usePagedConfigs(currentSleep, prevSleep, nextSleep, buildSleepConfig)

  if (!runsQuery.data) return <Skeleton className="h-64 w-full" />

  const noRunsInRange = currentRun.data.length === 0 && runsQuery.data.length > 0

  return (
    <div className="flex flex-col gap-4">
      <ChartPanel
        title="Training Load (Fitness / Fatigue / Form)"
        sub="CTL (42d fitness) & ATL (7d fatigue) vs. threshold-HR-based training load; TSB (form) — positive means fresh, negative means fatigued"
        empty={!pmc.current ? "Needs at least a couple of days of real training load (Phase 6.1's per-run TSS) to plot." : null}
      >
        {pmc.current && (
          <ChartCarousel
            prevConfig={pmc.prev}
            config={pmc.current}
            nextConfig={pmc.next}
            height={220}
            onPageShift={dragEnabled ? handlePageShift : undefined}
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
            empty={currentRun.outdoor.length < 2 ? "Log a few more outdoor runs across different temps." : null}
          >
            {tempHrConfig && <ChartCanvas config={tempHrConfig} height={120} />}
            {tempPaceConfig && <ChartCanvas config={tempPaceConfig} height={120} />}
            {tempCadConfig && <ChartCanvas config={tempCadConfig} height={120} />}
          </ChartPanel>

          <ChartPanel title="Weekly Mileage" empty={!mileage.current ? "Not enough data yet." : null}>
            {mileage.current && (
              <ChartCarousel
                prevConfig={mileage.prev}
                config={mileage.current}
                nextConfig={mileage.next}
                height={160}
                onPageShift={dragEnabled ? handlePageShift : undefined}
                loading={runsQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Pace, Cadence & HR Trend"
            sub="How your speed, turnover, and cardiac cost move together over time"
            empty={!perf.current ? "Need a couple more runs." : null}
          >
            {perf.current && (
              <>
                <ChartCarousel
                  prevConfig={perf.prev}
                  config={perf.current}
                  nextConfig={perf.next}
                  height={200}
                  onPageShift={dragEnabled ? handlePageShift : undefined}
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
            empty={!rollingPace.current ? "Need a couple more runs." : null}
          >
            {rollingPace.current && (
              <ChartCarousel
                prevConfig={rollingPace.prev}
                config={rollingPace.current}
                nextConfig={rollingPace.next}
                height={160}
                onPageShift={dragEnabled ? handlePageShift : undefined}
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
            empty={!steps.current ? "No step data synced yet (Garmin-only)." : null}
          >
            {steps.current && (
              <ChartCarousel
                prevConfig={steps.prev}
                config={steps.current}
                nextConfig={steps.next}
                height={140}
                onPageShift={dragEnabled ? handlePageShift : undefined}
                loading={stepsQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Resting Heart Rate"
            sub="Garmin wellness data"
            empty={!rhr.current ? "No resting HR data synced yet (Garmin-only)." : null}
          >
            {rhr.current && (
              <ChartCarousel
                prevConfig={rhr.prev}
                config={rhr.current}
                nextConfig={rhr.next}
                height={140}
                onPageShift={dragEnabled ? handlePageShift : undefined}
                loading={wellnessQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="VO2 Max"
            sub="Garmin wellness data — updates periodically, not every day"
            empty={!vo2.current ? "No VO2 max data synced yet (Garmin-only)." : null}
          >
            {vo2.current && (
              <ChartCarousel
                prevConfig={vo2.prev}
                config={vo2.current}
                nextConfig={vo2.next}
                height={140}
                onPageShift={dragEnabled ? handlePageShift : undefined}
                loading={wellnessQuery.isFetching}
              />
            )}
          </ChartPanel>

          <ChartPanel
            title="Sleep"
            sub="Sleep score and total duration"
            empty={!sleep.current ? "No sleep data synced yet (Garmin-only)." : null}
          >
            {sleep.current && (
              <ChartCarousel
                prevConfig={sleep.prev}
                config={sleep.current}
                nextConfig={sleep.next}
                height={160}
                onPageShift={dragEnabled ? handlePageShift : undefined}
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
