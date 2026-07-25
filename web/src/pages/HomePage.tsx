import { useNavigate } from "react-router-dom"
import { useDashboardSummary } from "@/hooks/useDashboardSummary"
import { useAllRuns } from "@/hooks/useRuns"
import { useGoals } from "@/hooks/useGoals"
import { useWellness } from "@/hooks/useWellness"
import { isRunActivity, isDistanceActivity, activityFamily, totalWeightLbLifted, ACTIVITY_VERBS } from "@/lib/runs"
import { paceStr, timeStr, fmtPctChange, fmtSleepDuration, isPlausiblePace } from "@/lib/format"
import type { WellnessDay } from "@/lib/api"
import { ChartCard, CardGrid } from "@/components/home/ChartCard"
import { DashBar } from "@/components/home/DashBar"
import { GoalCard } from "@/components/home/GoalCard"
import { FitnessTrendCard } from "@/components/home/FitnessTrendCard"
import { Skeleton } from "@/components/ui/skeleton"

const WEEK_MS = 7 * 86400000

// Two-source stat strip, ported from app.js's updateHeaderStats(): dashboard_summary's
// headerStats is small/cached and paints instantly, but doesn't account for the
// client-side Strava+Garmin duplicate merge (see lib/runs.ts) — so once the full
// /api/runs payload resolves, its merged, exact numbers replace the approximation.
function useStatStrip() {
  const dashboard = useDashboardSummary()
  const runsQuery = useAllRuns()
  const runs = runsQuery.data

  if (runs) {
    const runningRuns = runs.filter(isRunActivity)
    const weekAgo = Date.now() - WEEK_MS
    const weekMi = runningRuns
      .filter((r) => new Date(r.date).getTime() >= weekAgo)
      .reduce((s, r) => s + (r.distanceMi || 0), 0)

    const withPace = runningRuns.filter((r) => isPlausiblePace(r.avgPaceSecPerMi, r.distanceMi))
    const totalPaceDist = withPace.reduce((s, r) => s + (r.distanceMi || 0), 0)
    const avgPace = withPace.length
      ? withPace.reduce((s, r) => s + (r.avgPaceSecPerMi || 0) * (r.distanceMi || 0), 0) / totalPaceDist
      : null

    const byType: Record<string, { distanceMi: number; movingTimeSec: number; weightLb: number }> = {}
    runs
      .filter((r) => new Date(r.date).getTime() >= weekAgo)
      .forEach((r) => {
        const t = r.activityType || "Run"
        const bucket = (byType[t] ??= { distanceMi: 0, movingTimeSec: 0, weightLb: 0 })
        bucket.distanceMi += r.distanceMi || 0
        bucket.movingTimeSec += r.movingTimeSec || 0
        bucket.weightLb += totalWeightLbLifted(r)
      })

    return {
      loading: false,
      weekMi,
      avgPace,
      runCount: runningRuns.length,
      byType,
    }
  }

  const hs = dashboard.data?.headerStats
  return {
    loading: !hs,
    weekMi: hs?.weekMileageRun ?? 0,
    avgPace: hs?.avgPaceSecPerMiAllTime ?? null,
    runCount: hs?.runCountAllTime ?? 0,
    byType: null as Record<string, { distanceMi: number; movingTimeSec: number; weightLb: number }> | null,
  }
}

// Only worth a separate line when something other than running happened this week.
// Distance/weight/duration aren't comparable magnitudes, so each group sorts (and
// falls back) within its own unit rather than one combined ranking.
function statBreakdown(byType: Record<string, { distanceMi: number; movingTimeSec: number; weightLb: number }>) {
  const entries = Object.entries(byType)
  if (entries.length <= 1) return null

  const distanceParts = entries
    .filter(([t]) => isDistanceActivity(t))
    .sort((a, b) => b[1].distanceMi - a[1].distanceMi)
    .map(([t, v]) => `${ACTIVITY_VERBS[t] || t} ${v.distanceMi.toFixed(1)} mi`)
  const strengthParts = entries
    .filter(([t]) => !isDistanceActivity(t) && activityFamily(t) === "strength")
    .sort((a, b) => b[1].weightLb - a[1].weightLb)
    .map(([t, v]) =>
      v.weightLb > 0
        ? `${ACTIVITY_VERBS[t] || t} ${Math.round(v.weightLb).toLocaleString()} lbs`
        : `${ACTIVITY_VERBS[t] || t} ${timeStr(v.movingTimeSec)}`,
    )
  const timeParts = entries
    .filter(([t]) => !isDistanceActivity(t) && activityFamily(t) !== "strength")
    .sort((a, b) => b[1].movingTimeSec - a[1].movingTimeSec)
    .map(([t, v]) => `${ACTIVITY_VERBS[t] || t} ${timeStr(v.movingTimeSec)}`)

  return [...distanceParts, ...strengthParts, ...timeParts].join(" · ")
}

// Finds the pace-trend point closest to (but not after) 30 days before the most
// recent point — "vs about a month ago" without needing an exact 30-day-old sample.
function paceTrendDelta(paceTrend: { date: string; paceSecPerMi: number }[]) {
  if (!paceTrend.length) return { now: null as number | null, deltaSec: null as number | null }
  const latest = paceTrend[paceTrend.length - 1]
  const target = new Date(latest.date + "T00:00:00")
  target.setDate(target.getDate() - 30)
  let closest: { date: string; paceSecPerMi: number } | null = null
  for (const p of paceTrend) {
    if (new Date(p.date + "T00:00:00") <= target) closest = p
  }
  const deltaSec = closest ? Math.round(closest.paceSecPerMi - latest.paceSecPerMi) : null
  return { now: latest.paceSecPerMi, deltaSec }
}

function latestWellnessValue(data: WellnessDay[], field: keyof WellnessDay) {
  for (let i = data.length - 1; i >= 0; i--) {
    const v = data[i][field]
    if (v != null) return { value: v, date: data[i].date }
  }
  return null
}

// Exported for reuse by the Chat tab, which shows the same dashboard cards
// above the thread (mirrors legacy's renderChatTab() calling the same
// renderDashboardCards() helper Home uses).
export function DashboardCards() {
  const { data: d } = useDashboardSummary()
  const navigate = useNavigate()
  if (!d) return <Skeleton className="h-40 w-full" />

  const streak = d.consistencyStreak
  const load = d.trainingLoad
  const dsl = d.daysSinceLongestRun
  const pr = d.personalRecords
  const weekly = d.weeklyMileage
  const monthly = d.monthlyMileage

  const thisWeek = weekly.length ? weekly[weekly.length - 1] : null
  const avgWeek = weekly.length ? weekly.reduce((s, w) => s + w.totalMiles, 0) / weekly.length : 0
  const weekPct = thisWeek && avgWeek ? (thisWeek.totalMiles / avgWeek) * 100 : 0

  const loadColor =
    load.direction === "up" ? "var(--hale-hot)" : load.direction === "down" ? "var(--hale-cold)" : "var(--hale-good)"

  const { now: paceNow, deltaSec: paceDeltaSec } = paceTrendDelta(d.paceTrend)
  const paceColor = paceDeltaSec && paceDeltaSec > 0 ? "var(--hale-good)" : paceDeltaSec && paceDeltaSec < 0 ? "var(--hale-hot)" : undefined
  const paceLabel =
    paceDeltaSec == null ? "--" : paceDeltaSec === 0 ? "steady" : `${paceStr(Math.abs(paceDeltaSec))} ${paceDeltaSec > 0 ? "faster" : "slower"}`

  const thisMonth = monthly.length ? monthly[monthly.length - 1] : null
  const lastMonth = monthly.length > 1 ? monthly[monthly.length - 2] : null
  const monthPct = thisMonth && lastMonth && lastMonth.totalMiles ? (thisMonth.totalMiles / lastMonth.totalMiles) * 100 : 0

  return (
    <CardGrid>
      <ChartCard
        label="Consistency streak"
        value={`${streak.streakWeeks ?? 0} wk${streak.streakWeeks === 1 ? "" : "s"}`}
        bar={thisWeek ? <DashBar pct={weekPct} color="var(--hale-gold)" /> : undefined}
        breakdown={thisWeek ? `${thisWeek.totalMiles} mi this week` : "no data yet"}
        onClick={() => navigate("/activities?filter=week")}
      />
      <ChartCard
        label="4-week training load"
        value={fmtPctChange(load.pctChange)}
        valueColor={loadColor}
        breakdown={`${load.last28DaysMiles ?? "--"} mi vs ${load.prior28DaysMiles ?? "--"} mi prior`}
        onClick={() => navigate("/insights")}
      />
      <ChartCard
        label="Days since longest run"
        value={dsl ? dsl.days : "--"}
        breakdown={dsl ? `${dsl.distanceMi} mi on ${dsl.date}` : "no data yet"}
        onClick={dsl ? () => navigate(`/activities?run=${dsl.runId}`) : undefined}
      />
      <ChartCard
        label="Pace trend (~30d)"
        value={paceLabel}
        valueColor={paceColor}
        breakdown={paceNow != null ? `now ${paceStr(paceNow)}/mi` : "no data yet"}
        onClick={() => navigate("/insights")}
      />
      <ChartCard
        label="Longest run"
        value={pr.longestRun ? pr.longestRun.value.toFixed(2) + " mi" : "--"}
        breakdown={pr.longestRun ? pr.longestRun.date : ""}
        onClick={pr.longestRun ? () => navigate(`/activities?run=${pr.longestRun!.runId}`) : undefined}
      />
      <ChartCard
        label="Fastest pace"
        value={pr.fastestPace ? paceStr(pr.fastestPace.value) + "/mi" : "--"}
        breakdown={pr.fastestPace ? pr.fastestPace.date : ""}
        onClick={pr.fastestPace ? () => navigate(`/activities?run=${pr.fastestPace!.runId}`) : undefined}
      />
      <ChartCard
        label="This month vs last"
        value={thisMonth ? thisMonth.totalMiles + " mi" : "--"}
        bar={thisMonth && lastMonth ? <DashBar pct={monthPct} color="var(--hale-cold)" /> : undefined}
        breakdown={lastMonth ? `${lastMonth.totalMiles} mi last month` : ""}
        onClick={() => navigate("/activities?filter=month")}
      />
    </CardGrid>
  )
}

function WellnessCards() {
  const { data } = useWellness({ days: 30 })
  const navigate = useNavigate()
  if (!data) return null

  const rhr = latestWellnessValue(data, "restingHrBpm")
  const vo2 = latestWellnessValue(data, "vo2max")
  const sleepScore = latestWellnessValue(data, "sleepScore")
  const sleepDuration = latestWellnessValue(data, "sleepSeconds")
  // Garmin-only, optional bonus source — degrades to nothing (not an empty-state
  // card) when there's no data yet, matching the legacy renderWellnessCards().
  if (!rhr && !vo2 && !sleepScore) return null

  return (
    <div className="mt-6">
      <h2 className="mb-3 text-sm font-semibold">Wellness</h2>
      <CardGrid>
        <ChartCard
          label="Resting HR"
          value={rhr ? `${rhr.value} bpm` : "--"}
          breakdown={rhr ? String(rhr.date) : "no data yet"}
          onClick={() => navigate("/insights")}
        />
        <ChartCard
          label="VO2 max"
          value={vo2 ? String(vo2.value) : "--"}
          breakdown={vo2 ? String(vo2.date) : "no data yet"}
          onClick={() => navigate("/insights")}
        />
        <ChartCard
          label="Sleep"
          value={sleepScore ? String(sleepScore.value) : "--"}
          breakdown={`${sleepDuration ? fmtSleepDuration(sleepDuration.value as number) : ""} ${sleepScore ? `on ${sleepScore.date}` : "no data yet"}`}
          onClick={() => navigate("/insights")}
        />
      </CardGrid>
    </div>
  )
}

function GoalsSection() {
  const { data: goals } = useGoals()
  if (!goals) return <Skeleton className="h-24 w-full" />
  const active = goals.filter((g) => g.status === "active")
  if (!active.length) {
    return <div className="text-muted-foreground text-sm">No active goals yet — add one on the Goals tab.</div>
  }
  return (
    <CardGrid>
      {active.map((g) => (
        <GoalCard key={g.id} goal={g} linkToGoalsTab />
      ))}
    </CardGrid>
  )
}

export function HomePage() {
  const navigate = useNavigate()
  const { loading, weekMi, avgPace, runCount, byType } = useStatStrip()
  const breakdown = byType ? statBreakdown(byType) : null

  return (
    <div className="flex flex-col gap-6">
      <div>
        <CardGrid className="sm:grid-cols-3">
          <ChartCard
            label="This week"
            value={loading ? "--" : `${weekMi.toFixed(1)} mi`}
            onClick={() => navigate("/activities?filter=week")}
          />
          <ChartCard
            label="Avg pace (all)"
            value={loading ? "--" : avgPace ? `${paceStr(avgPace)}/mi` : "--"}
            onClick={() => navigate("/activities?filter=all")}
          />
          <ChartCard
            label="Runs logged"
            value={loading ? "--" : runCount}
            onClick={() => navigate("/activities?filter=all")}
          />
        </CardGrid>
        {breakdown && <div className="text-muted-foreground mt-2 font-mono text-xs">{breakdown}</div>}
      </div>

      <FitnessTrendCard />

      <div>
        <h2 className="mb-3 text-sm font-semibold">Goals</h2>
        <GoalsSection />
      </div>

      <DashboardCards />
      <WellnessCards />
    </div>
  )
}
