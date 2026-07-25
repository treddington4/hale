import { useMemo } from "react"
import { useNavigate } from "react-router-dom"
import type { ChartConfiguration } from "chart.js"
import { useMetrics } from "@/hooks/useWellness"
import type { DailyMetricPoint } from "@/lib/api"
import { Card } from "@/components/ui/card"
import { ChartCanvas } from "@/components/insights/ChartCanvas"
import { CHART_COLORS } from "@/lib/chartTheme"

const SPARKLINE_WINDOW_DAYS = 60
const TREND_LOOKBACK_DAYS = 28
// CTL points of drift over TREND_LOOKBACK_DAYS before it reads as "Building"/
// "Declining" rather than "Steady" — small enough to catch a real multi-week
// swing, large enough to ignore day-to-day noise (a hard week alone moves CTL
// by a few points at most, since it's a 42-day average — see pipeline.py).
const CTL_TREND_THRESHOLD = 3
// ATL points of drift over the trailing week before "Rising"/"Easing" rather
// than "Steady" — ATL is itself only a 7-day average, so this window is
// shorter than CTL's.
const ATL_TREND_THRESHOLD = 5

function pointNDaysBefore(points: DailyMetricPoint[], days: number) {
  const latest = points[points.length - 1]
  const target = new Date(latest.date + "T00:00:00")
  target.setDate(target.getDate() - days)
  let reference = points[0]
  for (const p of points) {
    if (new Date(p.date + "T00:00:00") <= target) reference = p
  }
  return reference
}

function trendWord(delta: number, threshold: number, upWord: string, downWord: string) {
  if (delta > threshold) return { word: upWord, arrow: "↑" }
  if (delta < -threshold) return { word: downWord, arrow: "↓" }
  return { word: "Steady", arrow: "→" }
}

// Same bands described in chat: TSB is the one PMC number that's directly
// interpretable on its own (it's already a relative difference, not a raw
// load level that only means something against your own history).
function formBand(tsb: number): { label: string; color: string } {
  if (tsb < -10) return { label: "Fatigued", color: "var(--hale-hot)" }
  if (tsb < 5) return { label: "Balanced", color: "var(--hale-good)" }
  if (tsb <= 25) return { label: "Fresh", color: "var(--hale-good)" }
  return { label: "Very fresh", color: "var(--hale-cold)" }
}

export function FitnessTrendCard() {
  const navigate = useNavigate()
  const metricsQuery = useMetrics({ days: SPARKLINE_WINDOW_DAYS })
  const points = metricsQuery.data

  const sparklineConfig = useMemo<ChartConfiguration | null>(() => {
    if (!points || points.length < 2) return null
    return {
      type: "line",
      data: {
        labels: points.map((p) => p.date),
        datasets: [
          {
            data: points.map((p) => p.ctl),
            borderColor: CHART_COLORS.gold,
            backgroundColor: CHART_COLORS.gold,
            tension: 0.25,
            pointRadius: 0,
            borderWidth: 2,
          },
        ],
      },
      options: {
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
      },
    }
  }, [points])

  // Not enough training history for CTL/ATL/TSB to mean anything yet — same
  // gating message as the Insights PMC chart's own empty state, so skip the
  // card entirely rather than showing a half-populated one.
  if (!points || points.length < 2) return null

  const latest = points[points.length - 1]
  const ctlRef = pointNDaysBefore(points, TREND_LOOKBACK_DAYS)
  const atlRef = pointNDaysBefore(points, 7)
  const fitnessTrend = trendWord(latest.ctl - ctlRef.ctl, CTL_TREND_THRESHOLD, "Building", "Declining")
  const fatigueTrend = trendWord(latest.atl - atlRef.atl, ATL_TREND_THRESHOLD, "Rising", "Easing")
  const form = formBand(latest.tsb)

  return (
    <Card
      onClick={() => navigate("/insights")}
      className="hover:border-muted-foreground/40 cursor-pointer gap-3 transition-colors"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="text-muted-foreground text-xs font-medium tracking-wider uppercase">
          Fitness Trend
        </div>
        <div className="text-muted-foreground text-xs">Fitness (CTL) · Fatigue (ATL) · Form (TSB)</div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div>
          <div className="text-muted-foreground text-xs">Fitness</div>
          <div className="font-mono text-lg font-bold tabular-nums">{Math.round(latest.ctl)}</div>
          <div
            className="text-xs font-medium"
            style={{ color: fitnessTrend.word === "Declining" ? "var(--hale-cold)" : fitnessTrend.word === "Building" ? "var(--hale-good)" : undefined }}
          >
            {fitnessTrend.arrow} {fitnessTrend.word}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground text-xs">Fatigue</div>
          <div className="font-mono text-lg font-bold tabular-nums">{Math.round(latest.atl)}</div>
          <div
            className="text-xs font-medium"
            style={{ color: fatigueTrend.word === "Rising" ? "var(--hale-hot)" : fatigueTrend.word === "Easing" ? "var(--hale-good)" : undefined }}
          >
            {fatigueTrend.arrow} {fatigueTrend.word}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground text-xs">Form</div>
          <div className="font-mono text-lg font-bold tabular-nums">{Math.round(latest.tsb)}</div>
          <div className="text-xs font-medium" style={{ color: form.color }}>
            {form.label}
          </div>
        </div>
      </div>

      {sparklineConfig && (
        <div>
          <div className="text-muted-foreground mb-1 text-xs">
            Fitness over the last {SPARKLINE_WINDOW_DAYS} days — the slope is what matters, not the raw number
          </div>
          <ChartCanvas config={sparklineConfig} height={48} />
        </div>
      )}
    </Card>
  )
}
