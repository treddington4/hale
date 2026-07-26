// Location clustering for the Map tab, ported 1:1 from app.js's clusterRuns/
// clusterCentroid. Entirely client-side and greedy: the first run seeds a
// cluster, subsequent runs join the nearest existing cluster if within
// CLUSTER_RADIUS_KM, else seed a new one. No server-side location endpoint
// exists — see api.geocode() for the (server-cached) reverse-geocoding used to
// label a cluster's centroid.
import { haversineKm } from "@/lib/route"
import type { Run } from "@/lib/activities"

const CLUSTER_RADIUS_KM = 50

export interface MapItem {
  run: Run
  route: [number, number][]
}

export interface MapCluster {
  id: string
  anchor: [number, number]
  items: MapItem[]
}

export function clusterRuns(items: MapItem[]): MapCluster[] {
  const clusters: MapCluster[] = []
  items.forEach((item) => {
    const start = item.route[0]
    const cluster = clusters.find((c) => haversineKm(c.anchor, start) <= CLUSTER_RADIUS_KM)
    if (cluster) {
      cluster.items.push(item)
    } else {
      clusters.push({ id: `c${clusters.length}`, anchor: start, items: [item] })
    }
  })
  clusters.sort((a, b) => b.items.length - a.items.length)
  return clusters
}

export function clusterCentroid(cluster: MapCluster): [number, number] {
  const lat = cluster.items.reduce((s, i) => s + i.route[0][0], 0) / cluster.items.length
  const lon = cluster.items.reduce((s, i) => s + i.route[0][1], 0) / cluster.items.length
  return [lat, lon]
}
