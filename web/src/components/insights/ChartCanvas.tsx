import { useEffect, useRef } from "react"
import Chart from "chart.js/auto"
import type { ChartConfiguration } from "chart.js"

// One Chart.js instance per mounted canvas, created/destroyed via effect
// cleanup rather than the legacy global `charts` array + manual destroyCharts()
// — React's unmount timing means per-canvas cleanup is the safer equivalent
// (see PLAN.md 0.6 notes). Callers must useMemo `config` so unrelated re-renders
// don't tear down and rebuild the chart on every render.
//
// Drag/swipe-to-scroll used to live here as a CSS-transform trick, but that
// only ever slid the already-rendered image — dragging past its edge exposed
// blank canvas since there was nothing rendered there to reveal. That's now
// ChartCarousel.tsx's job: it pre-renders real prev/current/next pages side
// by side, so panning it reveals genuine data. This component stays the
// plain single-chart building block for everything that doesn't page
// (scatter charts, the sleep hypnogram) and is used internally by
// ChartCarousel's own panes too.
export function ChartCanvas({
  config,
  height = 200,
  loading = false,
}: {
  config: ChartConfiguration
  height?: number
  // True while the query backing this chart's data is refetching. Fades the
  // canvas out/in over the swap instead of the new dataset popping in as a
  // hard cut.
  loading?: boolean
}) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = new Chart(ref.current, config)
    // Double-click resets pan/zoom (chartjs-plugin-zoom, see chartTheme.ts's
    // CHART_PAN_ZOOM) — a no-op via optional chaining on charts that don't have
    // the zoom plugin's options set, so this is safe to attach unconditionally
    // rather than needing every caller to wire it up individually.
    const canvas = ref.current
    const resetZoom = () => chart.resetZoom?.()
    canvas.addEventListener("dblclick", resetZoom)
    return () => {
      canvas.removeEventListener("dblclick", resetZoom)
      chart.destroy()
    }
  }, [config])

  return (
    <div
      className="mt-2 overflow-hidden transition-opacity duration-200"
      style={{ opacity: loading ? 0.35 : 1 }}
    >
      <canvas ref={ref} height={height} />
    </div>
  )
}
