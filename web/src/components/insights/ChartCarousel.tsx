import { useEffect, useLayoutEffect, useRef } from "react"
import Chart from "chart.js/auto"
import type { ChartConfiguration } from "chart.js"

function CarouselPane({ config, height }: { config: ChartConfiguration | null; height: number }) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!ref.current || !config) return
    // Chart.js's own responsive:true (the default) ResizeObserver watches the
    // canvas's parent here — but nested inside a 300%-wide flex row (see the
    // REST/TO_PREV/TO_NEXT comment below), its own measurement intermittently
    // locked onto the canvas's ~300px browser-default width and then
    // re-applied that same wrong number every time it fired again, actively
    // fighting our own explicit resize below (confirmed live: our resize call
    // landed with the correct measured width, then Chart.js's own observer
    // immediately overwrote it back to 300 on the very next tick). Disabling
    // Chart.js's own responsive sizing for these panes and driving size
    // entirely from our own parent measurement removes that fight.
    const chart = new Chart(ref.current, {
      ...config,
      options: { ...config.options, responsive: false, maintainAspectRatio: false },
    })
    const parent = ref.current.parentElement
    const resizeToParent = () => {
      if (!parent) return
      const { clientWidth, clientHeight } = parent
      if (clientWidth > 0 && clientHeight > 0) chart.resize(clientWidth, clientHeight)
    }
    const raf = requestAnimationFrame(resizeToParent)
    const ro = parent ? new ResizeObserver(resizeToParent) : null
    if (parent && ro) ro.observe(parent)
    return () => {
      cancelAnimationFrame(raf)
      ro?.disconnect()
      chart.destroy()
    }
  }, [config])

  return (
    <div className="shrink-0" style={{ width: "33.3333%", height }}>
      {config ? (
        <canvas ref={ref} height={height} />
      ) : (
        <div className="text-hale-faint flex h-full items-center justify-center text-xs">No data loaded</div>
      )}
    </div>
  )
}

// Rest position: shift the 3-pane row left by exactly one pane so the middle
// (current) pane fills the viewport. Percentages here are relative to the
// row's OWN width (300% of the container, per the panes' 33.3333% widths),
// so -33.3333% = one pane, -66.6667% = two panes — CSS calc() lets us layer a
// live pixel drag offset on top of that percentage without any JS width
// measurement (no ResizeObserver, no stored pixel state — recomputes
// correctly on any layout change for free).
const REST = "-33.3333%"
const TO_PREV = "0%"
const TO_NEXT = "-66.6667%"
const RELEASE_TRANSITION = "transform 220ms cubic-bezier(0.22, 1, 0.36, 1)"
const COMMIT_FRACTION = 0.3
const DRAG_RESIST = 0.15 // how much a drag toward an unloaded/unavailable page is damped, for a rubber-band feel instead of a hard stop

// Swipe/drag-to-page: pre-fetched, already-rendered prev/current/next charts
// slide as one row so dragging reveals real data instead of the blank canvas
// the old CSS-transform-only drag exposed (see ChartCanvas.tsx's comment).
// Past COMMIT_FRACTION of the container's width, release commits to a full
// page shift (matching FilterBar's own prev/next step); short of that, it
// eases back to center.
export function ChartCarousel({
  prevConfig,
  config,
  nextConfig,
  height = 200,
  loading = false,
  onPageShift,
}: {
  prevConfig: ChartConfiguration | null
  config: ChartConfiguration | null
  nextConfig: ChartConfiguration | null
  height?: number
  loading?: boolean
  onPageShift?: (direction: -1 | 1) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const rowRef = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const row = rowRef.current
    const container = containerRef.current
    if (!row || !container) return

    row.style.transition = "none"
    row.style.transform = `translateX(${REST})`

    if (!onPageShift) return

    let dragging = false
    let startX = 0
    let containerWidth = 0

    const begin = (clientX: number) => {
      dragging = true
      startX = clientX
      containerWidth = container.getBoundingClientRect().width
      row.style.transition = "none"
      container.style.cursor = "grabbing"
    }
    const move = (clientX: number) => {
      if (!dragging) return
      let dx = clientX - startX
      if (dx > 0 && !prevConfig) dx *= DRAG_RESIST
      if (dx < 0 && !nextConfig) dx *= DRAG_RESIST
      row.style.transform = `translateX(calc(${REST} + ${dx}px))`
    }
    const end = (clientX: number) => {
      if (!dragging) return
      dragging = false
      container.style.cursor = "grab"
      const dx = clientX - startX
      const frac = containerWidth > 0 ? dx / containerWidth : 0
      row.style.transition = RELEASE_TRANSITION
      if (frac > COMMIT_FRACTION && prevConfig) {
        row.style.transform = `translateX(${TO_PREV})`
        window.setTimeout(() => onPageShift(-1), 220)
      } else if (frac < -COMMIT_FRACTION && nextConfig) {
        row.style.transform = `translateX(${TO_NEXT})`
        window.setTimeout(() => onPageShift(1), 220)
      } else {
        row.style.transform = `translateX(${REST})`
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

    container.style.cursor = "grab"
    container.addEventListener("mousedown", onMouseDown)
    window.addEventListener("mousemove", onMouseMove)
    window.addEventListener("mouseup", onMouseUp)
    container.addEventListener("touchstart", onTouchStart, { passive: true })
    container.addEventListener("touchmove", onTouchMove, { passive: true })
    container.addEventListener("touchend", onTouchEnd)

    return () => {
      container.removeEventListener("mousedown", onMouseDown)
      window.removeEventListener("mousemove", onMouseMove)
      window.removeEventListener("mouseup", onMouseUp)
      container.removeEventListener("touchstart", onTouchStart)
      container.removeEventListener("touchmove", onTouchMove)
      container.removeEventListener("touchend", onTouchEnd)
    }
  }, [prevConfig, config, nextConfig, onPageShift])

  return (
    <div
      ref={containerRef}
      className="mt-2 overflow-hidden transition-opacity duration-200"
      style={{ opacity: loading ? 0.35 : 1 }}
    >
      <div ref={rowRef} className="flex" style={{ width: "300%" }}>
        <CarouselPane config={prevConfig} height={height} />
        <CarouselPane config={config} height={height} />
        <CarouselPane config={nextConfig} height={height} />
      </div>
    </div>
  )
}
