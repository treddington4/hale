import { useEffect, useMemo, useRef, useState } from "react"
import L from "leaflet"
import { useAllRuns } from "@/hooks/useActivities"
import { useHrFloor, isPlausibleHR } from "@/hooks/useHrFloor"
import { api } from "@/lib/api"
import { clusterRuns, clusterCentroid, type MapItem } from "@/lib/mapClusters"
import { splitRouteAtGaps } from "@/lib/route"
import { METRIC_CONFIG, HEAT_BUCKETS, buildMetricSegments, heatColor, type MetricMode } from "@/lib/mapHeat"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

const TILE_URL = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'

export function MapPage() {
  const allRunsQuery = useAllRuns()
  const hrFloor = useHrFloor()

  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<L.Map | null>(null)
  const routeLayerRef = useRef<L.FeatureGroup | null>(null)
  const metricLayerRef = useRef<L.FeatureGroup | null>(null)
  const autoCenterApplied = useRef(false)

  const [selectedLocation, setSelectedLocation] = useState<string>("all")
  const [metric, setMetric] = useState<MetricMode>("density")
  const [clusterLabels, setClusterLabels] = useState<Record<string, string>>({})
  const [summary, setSummary] = useState("")

  // Some activities have no summary_polyline from Strava (a data quirk, not a sync
  // bug) but still have real GPS via the streams-derived routeMetrics — fall back
  // to deriving a route from that rather than dropping the activity from the map.
  const items = useMemo<MapItem[]>(() => {
    const runs = allRunsQuery.data ?? []
    return runs
      .map((r) => ({
        run: r,
        route:
          r.route && r.route.length > 1
            ? r.route
            : (r.routeMetrics || []).map((p) => [p.lat, p.lon] as [number, number]),
      }))
      .filter((item) => item.route.length > 1)
  }, [allRunsQuery.data])

  const clusters = useMemo(() => clusterRuns(items), [items])

  // Defaults the map to the location cluster containing the most recent activity
  // (rather than always "All locations", which zooms out enough to include any
  // one-off travel runs) — applied only once per page load so it never overrides
  // a location the user has since picked manually.
  useEffect(() => {
    if (autoCenterApplied.current || !clusters.length) return
    autoCenterApplied.current = true
    let mostRecentKey = ""
    let mostRecentClusterId = ""
    clusters.forEach((c) => {
      c.items.forEach((item) => {
        const key = `${item.run.date}T${item.run.startTime || ""}`
        if (key > mostRecentKey) {
          mostRecentKey = key
          mostRecentClusterId = c.id
        }
      })
    })
    if (mostRecentClusterId) setSelectedLocation(mostRecentClusterId)
  }, [clusters])

  // Sequential background reverse-geocoding of each cluster's centroid, matching
  // the server's Nominatim rate limit (app/main.py's /api/geocode) — a cache hit
  // (the common case after the first real lookup) resolves near-instantly.
  useEffect(() => {
    let cancelled = false
    async function run() {
      for (const cluster of clusters) {
        if (cancelled) return
        if (clusterLabels[cluster.id]) continue
        const [lat, lon] = clusterCentroid(cluster)
        try {
          const { label } = await api.geocode(lat, lon)
          if (!cancelled) setClusterLabels((prev) => ({ ...prev, [cluster.id]: label }))
        } catch {
          if (!cancelled) setClusterLabels((prev) => ({ ...prev, [cluster.id]: `${lat.toFixed(2)}, ${lon.toFixed(2)}` }))
        }
        return // one lookup per effect run; the state update above re-triggers this effect for the next
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [clusters, clusterLabels])

  // Leaflet map instance: created once per mount, destroyed on unmount — the React
  // equivalent of legacy's module-level `if (!map)` singleton (which persisted
  // across tab switches since the legacy app never unmounts tab content, just
  // toggles display:none). Since this component genuinely mounts/unmounts with
  // route navigation, create-on-mount/destroy-on-unmount is the correct mapping.
  useEffect(() => {
    if (!containerRef.current) return
    const map = L.map(containerRef.current, { preferCanvas: true }).setView([40, -97], 4)
    L.tileLayer(TILE_URL, { attribution: TILE_ATTRIBUTION, maxZoom: 19 }).addTo(map)
    const routeLayer = L.featureGroup()
    mapRef.current = map
    routeLayerRef.current = routeLayer
    return () => {
      map.remove()
      mapRef.current = null
      routeLayerRef.current = null
      metricLayerRef.current = null
    }
  }, [])

  // Redraw whenever the filtered item set or metric mode changes — mirrors
  // legacy's drawMapView().
  useEffect(() => {
    const map = mapRef.current
    const routeLayer = routeLayerRef.current
    if (!map || !routeLayer) return

    if (!items.length) {
      routeLayer.clearLayers()
      if (metricLayerRef.current) {
        map.removeLayer(metricLayerRef.current)
        metricLayerRef.current = null
      }
      setSummary(
        !allRunsQuery.data
          ? "Loading…"
          : allRunsQuery.data.length > 0
            ? "No GPS routes yet. Run a Backlog Sync from Settings to backfill routes for existing runs (this feature was added after they were first synced)."
            : "No runs yet.",
      )
      const raf = requestAnimationFrame(() => map.invalidateSize())
      return () => cancelAnimationFrame(raf)
    }

    const filteredItems =
      selectedLocation === "all"
        ? clusters.flatMap((c) => c.items)
        : (clusters.find((c) => c.id === selectedLocation)?.items ?? [])

    let boundsLayer: L.FeatureGroup | null = null

    if (metric === "density") {
      if (metricLayerRef.current) {
        map.removeLayer(metricLayerRef.current)
        metricLayerRef.current = null
      }
      if (!map.hasLayer(routeLayer)) routeLayer.addTo(map)
      routeLayer.clearLayers()
      filteredItems.forEach(({ route }) => {
        splitRouteAtGaps(route).forEach((segment) => {
          L.polyline(segment, { color: "#FFC857", weight: 2, opacity: 0.22, interactive: false }).addTo(routeLayer)
        })
      })
      boundsLayer = routeLayer

      const totalWithRoutes = clusters.reduce((s, c) => s + c.items.length, 0)
      setSummary(
        selectedLocation === "all"
          ? `${totalWithRoutes} run${totalWithRoutes === 1 ? "" : "s"} with GPS data plotted (of ${allRunsQuery.data?.length ?? 0} total)`
          : `${filteredItems.length} run${filteredItems.length === 1 ? "" : "s"} plotted for this location`,
      )
    } else {
      if (map.hasLayer(routeLayer)) map.removeLayer(routeLayer)
      if (metricLayerRef.current) {
        map.removeLayer(metricLayerRef.current)
        metricLayerRef.current = null
      }

      const cfg = METRIC_CONFIG[metric]
      const isValid = metric === "hr" ? (v: number) => isPlausibleHR(v, hrFloor) : undefined
      const { buckets, min, max, runCount } = buildMetricSegments(filteredItems, cfg, isValid)

      if (runCount) {
        const layer = L.featureGroup()
        buckets.forEach((segments, i) => {
          if (!segments.length) return
          const t = (i + 0.5) / HEAT_BUCKETS
          L.polyline(segments, { color: heatColor(t, cfg.gradient), weight: 3, opacity: 0.85, interactive: false }).addTo(layer)
        })
        layer.addTo(map)
        metricLayerRef.current = layer
        boundsLayer = layer
        setSummary(`${cfg.label} · ${runCount} run${runCount === 1 ? "" : "s"} · ${cfg.legend(min, max, cfg)}`)
      } else {
        setSummary(
          `No ${cfg.label.toLowerCase()} data yet for this selection — this is Strava-only for now, and needs a Backlog Sync to backfill runs synced before this feature existed.`,
        )
      }
    }

    const raf = requestAnimationFrame(() => {
      map.invalidateSize()
      if (boundsLayer) {
        const bounds = boundsLayer.getBounds()
        if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] })
      }
    })
    return () => cancelAnimationFrame(raf)
  }, [items, clusters, selectedLocation, metric, hrFloor, allRunsQuery.data])

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        {items.length > 0 && (
          <>
            <Select value={selectedLocation} onValueChange={setSelectedLocation}>
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All locations · {clusters.reduce((s, c) => s + c.items.length, 0)} runs</SelectItem>
                {clusters.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {clusterLabels[c.id] ?? "Locating…"} · {c.items.length} run{c.items.length === 1 ? "" : "s"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={metric} onValueChange={(v) => setMetric(v as MetricMode)}>
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="density">Density</SelectItem>
                <SelectItem value="pace">Pace</SelectItem>
                <SelectItem value="hr">Heart Rate</SelectItem>
                <SelectItem value="cadence">Cadence</SelectItem>
                <SelectItem value="elevation">Grade</SelectItem>
              </SelectContent>
            </Select>
          </>
        )}
        <div className="text-muted-foreground font-mono text-xs">{summary}</div>
      </div>
      <div ref={containerRef} className="border-border bg-card h-[480px] w-full rounded-xl border" />
    </div>
  )
}
