import { useEffect, useRef } from "react"
import Chart from "chart.js/auto"
import type { ChartConfiguration } from "chart.js"

// One Chart.js instance per mounted canvas, created/destroyed via effect
// cleanup rather than the legacy global `charts` array + manual destroyCharts()
// — React's unmount timing means per-canvas cleanup is the safer equivalent
// (see PLAN.md 0.6 notes). Callers must useMemo `config` so unrelated re-renders
// don't tear down and rebuild the chart on every render.
export function ChartCanvas({
  config,
  height = 200,
  onWindowDrag,
  loading = false,
}: {
  config: ChartConfiguration
  height?: number
  // Called once per completed drag/swipe with the horizontal fraction of the
  // canvas width dragged (positive = dragged right = "go back in time",
  // negative = dragged left = "go forward in time"). Callers use this to shift
  // their own fetched date range and refetch — this is deliberately NOT
  // chartjs-plugin-zoom's built-in pan (which only slides the view within
  // data that's already loaded); dragging here changes what's loaded.
  onWindowDrag?: (fraction: number) => void
  // True while the query backing this chart's data is refetching after a
  // window-drag/FilterBar change. Fades the canvas out/in over the swap
  // instead of the new dataset popping in as a hard cut — see the
  // release-snap comment below for why the drag gesture itself needed the
  // same "ease instead of jump-cut" treatment.
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

    let dragging = false
    let startX = 0
    const DRAG_THRESHOLD_PX = 20
    const RELEASE_TRANSITION = "transform 220ms cubic-bezier(0.22, 1, 0.36, 1)"

    const begin = (clientX: number) => {
      dragging = true
      startX = clientX
      canvas.style.transition = "none" // live 1:1 tracking while actually dragging
      canvas.style.cursor = "grabbing"
    }
    const move = (clientX: number) => {
      if (!dragging) return
      canvas.style.transform = `translateX(${clientX - startX}px)`
    }
    const end = (clientX: number) => {
      if (!dragging) return
      dragging = false
      const deltaPx = clientX - startX
      const width = canvas.getBoundingClientRect().width
      // Ease back to center instead of an instant snap — the snap-back
      // reads as a "flash" when it happens the same instant the underlying
      // data starts refetching (see PLAN.md 6.2.1 follow-up).
      canvas.style.transition = RELEASE_TRANSITION
      canvas.style.transform = ""
      canvas.style.cursor = "grab"
      window.setTimeout(() => {
        canvas.style.transition = ""
      }, 220)
      if (onWindowDrag && width > 0 && Math.abs(deltaPx) > DRAG_THRESHOLD_PX) {
        onWindowDrag(deltaPx / width)
      }
    }

    const onMouseDown = (e: MouseEvent) => begin(e.clientX)
    const onMouseMove = (e: MouseEvent) => move(e.clientX)
    const onMouseUp = (e: MouseEvent) => end(e.clientX)
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 1) begin(e.touches[0].clientX)
    }
    const onTouchMove = (e: TouchEvent) => {
      if (e.touches.length === 1) move(e.touches[0].clientX)
    }
    const onTouchEnd = (e: TouchEvent) => end(e.changedTouches[0]?.clientX ?? startX)

    if (onWindowDrag) {
      canvas.style.cursor = "grab"
      canvas.addEventListener("mousedown", onMouseDown)
      window.addEventListener("mousemove", onMouseMove)
      window.addEventListener("mouseup", onMouseUp)
      canvas.addEventListener("touchstart", onTouchStart, { passive: true })
      canvas.addEventListener("touchmove", onTouchMove, { passive: true })
      canvas.addEventListener("touchend", onTouchEnd)
    }

    return () => {
      canvas.removeEventListener("dblclick", resetZoom)
      if (onWindowDrag) {
        canvas.removeEventListener("mousedown", onMouseDown)
        window.removeEventListener("mousemove", onMouseMove)
        window.removeEventListener("mouseup", onMouseUp)
        canvas.removeEventListener("touchstart", onTouchStart)
        canvas.removeEventListener("touchmove", onTouchMove)
        canvas.removeEventListener("touchend", onTouchEnd)
      }
      chart.destroy()
    }
  }, [config, onWindowDrag])

  return (
    <div
      className="mt-2 overflow-hidden transition-opacity duration-200"
      style={{ opacity: loading ? 0.35 : 1 }}
    >
      <canvas ref={ref} height={height} />
    </div>
  )
}
