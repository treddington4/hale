// Run type + client-side duplicate-merge logic, ported 1:1 from app/static/app.js
// (mergeDuplicateRuns/isLikelyDuplicate/mergeRunPair/canonicalActivityType). Two
// independent sync sources (Strava, Garmin) write the same physical run as two
// separate rows — see CLAUDE.md's "Two independent sync sources" section — and this
// merge only ever happens client-side at display time, never in storage. Keep this
// in sync with the legacy implementation if the merge heuristic ever changes; the
// legacy file stays authoritative until Phase 0.10 (cutover) retires it.

export interface ExerciseSet {
  exercise: string
  setType: string | null
  reps: number | null
  weightLb: number | null
  durationSec: number | null
  supersetGroup: string | null
}

export interface RunSplit {
  mile: number
  paceSecPerMi: number | null
  elevGainFt: number | null
  avgHR: number | null
  maxHR: number | null
  avgCadence: number | null
}

export type IntervalSegment = "warmup" | "work" | "recovery" | "cooldown"

export interface IntervalRep {
  durationSec: number | null
  distanceMi: number | null
  paceSecPerMi: number | null
  elevGainFt: number | null
  avgHR: number | null
  maxHR: number | null
  avgCadence: number | null
  elapsedTimeSec: number | null
  segment: IntervalSegment | string
}

export interface RecoveryRep {
  repIndex: number
  recoverySec: number | null
}

export interface RouteMetricPoint {
  lat: number
  lon: number
  paceSecPerMi: number | null
  hr: number | null
  cadence: number | null
  gradePct: number | null
}

// Fields used by the Phase 0.3 (Home) and 0.5 (Activities) ports. Widened with an
// index signature so fields no tab consumes yet (0.7 Map's route rendering needs
// nothing beyond `route`/`routeMetrics`, already typed here) still pass through.
export interface Run {
  id: string
  source: "strava" | "garmin"
  activityType: string
  date: string
  startTime: string | null
  name: string
  distanceMi: number | null
  movingTimeSec: number | null
  elevGainFt: number | null
  avgHR: number | null
  maxHR: number | null
  avgCadence: number | null
  avgPaceSecPerMi: number | null
  isTreadmill: boolean
  tempF: number | null
  weatherCondition: string | null
  heatIndexF: number | null
  wetBulbF: number | null
  suggestedType: string | null
  type: string | null
  rpe: number | null
  notes: string | null
  exerciseSets: ExerciseSet[] | null
  splits: RunSplit[] | null
  intervals: IntervalRep[]
  recovery: RecoveryRep[]
  route: [number, number][]
  routeMetrics: RouteMetricPoint[]
  verticalOscillationMm: number | null
  groundContactTimeMs: number | null
  verticalRatioPct: number | null
  strideLengthM: number | null
  avgPowerWatts: number | null
  gearId: string | null
  mergedSources?: string[]
  mergedIds?: string[]
  [key: string]: unknown
}

export const RUN_TYPES = ["Easy", "Tempo", "Interval", "Long Run", "Recovery", "Hill", "Race"]
export const STRENGTH_TYPES = ["Full Body", "Upper Body", "Lower Body", "Push", "Pull", "Legs", "Core", "Other"]

export const TYPE_COLORS: Record<string, string> = {
  Easy: "#5FD68A",
  Tempo: "#FFC857",
  Interval: "rgb(255,107,53)",
  "Long Run": "rgb(76,201,240)",
  Recovery: "#5A6270",
  Hill: "#B98CE0",
  Race: "#FF4D6D",
}

export function canonicalActivityType(t: string | null | undefined): string {
  const s = (t || "").toLowerCase()
  if (s.includes("run")) return "run"
  if (s.includes("walk")) return "walk"
  if (s.includes("ride") || s.includes("cycl") || s.includes("bik")) return "ride"
  if (s.includes("swim")) return "swim"
  if (s.includes("hik")) return "hike"
  if (s.includes("weight") || s.includes("strength")) return "strength"
  if (s.includes("yoga")) return "yoga"
  return s
}

export function activityFamily(activityType: string | null | undefined): string {
  const t = (activityType || "run").toLowerCase()
  if (t.includes("run")) return "run"
  if (t.includes("strength") || t.includes("weight")) return "strength"
  if (t.includes("cycl") || t === "ride") return "ride"
  if (t.includes("walk")) return "walk"
  if (t.includes("hik")) return "hike"
  if (t.includes("swim")) return "swim"
  return "other"
}

const DISTANCE_FAMILIES = new Set(["run", "ride", "walk", "hike", "swim"])
export function isDistanceActivity(activityType: string | null | undefined): boolean {
  return DISTANCE_FAMILIES.has(activityFamily(activityType))
}

export function isRunActivity(r: Run): boolean {
  return (r.activityType || "Run") === "Run"
}

// Sum of reps*weightLb across a run's exerciseSets (Garmin-only, strength_training).
// Bodyweight/unknown-exercise sets carry weightLb: null and don't contribute.
export function totalWeightLbLifted(run: Run): number {
  if (!run.exerciseSets) return 0
  return run.exerciseSets.reduce(
    (sum, s) => sum + (s.weightLb != null && s.reps != null ? s.weightLb * s.reps : 0),
    0,
  )
}

function isEmptyValue(v: unknown): boolean {
  return v == null || (Array.isArray(v) && v.length === 0)
}

function isLikelyDuplicate(a: Run, b: Run): boolean {
  if (a.source === b.source) return false
  if (a.date !== b.date) return false
  if (canonicalActivityType(a.activityType) !== canonicalActivityType(b.activityType)) return false
  if (a.distanceMi == null || b.distanceMi == null) return false
  if (Math.abs(a.distanceMi - b.distanceMi) > Math.max(0.1, a.distanceMi * 0.05)) return false
  if (a.startTime && b.startTime) {
    const toMin = (t: string) => {
      const [h, m] = t.split(":").map(Number)
      return h * 60 + m
    }
    if (Math.abs(toMin(a.startTime) - toMin(b.startTime)) > 10) return false
  }
  return true
}

// Strava preferred where it has data (better route/routeMetrics); Garmin fills in
// anything Strava lacks (mainly running-dynamics fields, which Strava never
// populates). Exception: Garmin's activity names are usually more descriptive than
// Strava's generic auto-names, so they win regardless of the general merge order.
function mergeRunPair(a: Run, b: Run): Run {
  const primary = a.source === "strava" ? a : b.source === "strava" ? b : a
  const secondary = primary === a ? b : a
  const merged: Run = { ...secondary }
  Object.entries(primary).forEach(([k, v]) => {
    if (!isEmptyValue(v)) (merged as Record<string, unknown>)[k] = v
  })
  const garminSide = a.source === "garmin" ? a : b.source === "garmin" ? b : null
  if (garminSide && !isEmptyValue(garminSide.name)) merged.name = garminSide.name
  merged.mergedSources = [a.source, b.source].sort()
  merged.mergedIds = [a.id, b.id]
  return merged
}

export function mergeDuplicateRuns(rawRuns: Run[]): Run[] {
  const used = new Array(rawRuns.length).fill(false)
  const merged: Run[] = []
  for (let i = 0; i < rawRuns.length; i++) {
    if (used[i]) continue
    let matchIdx = -1
    for (let j = i + 1; j < rawRuns.length; j++) {
      if (used[j]) continue
      if (isLikelyDuplicate(rawRuns[i], rawRuns[j])) {
        matchIdx = j
        break
      }
    }
    if (matchIdx >= 0) {
      used[matchIdx] = true
      merged.push(mergeRunPair(rawRuns[i], rawRuns[matchIdx]))
    } else {
      merged.push(rawRuns[i])
    }
  }
  return merged
}

export const ACTIVITY_VERBS: Record<string, string> = {
  Run: "Ran",
  TrailRun: "Ran",
  Ride: "Biked",
  VirtualRide: "Biked",
  MountainBikeRide: "Biked",
  Walk: "Walked",
  Hike: "Hiked",
  Swim: "Swam",
  Workout: "Worked out",
  WeightTraining: "Lifted",
  strength_training: "Lifted",
  Yoga: "Did yoga",
  Elliptical: "Did elliptical",
}
