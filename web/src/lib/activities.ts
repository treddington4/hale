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
  source: "strava" | "garmin" | "hevy"
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
  return canonicalActivityType(r.activityType) === "run"
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

// Pure date/type/distance/time proximity check -- deliberately does NOT check
// source distinctness itself (unlike the pre-Phase-25 version). That's the
// caller's job now: mergeDuplicateRuns applies a stricter rule for two
// same-source candidates (only join a group with third-party corroboration)
// than for cross-source ones, since some accounts have BOTH Garmin->Strava
// and Hevy->Strava auto-posting independently for the same physical workout
// (confirmed live) -- two genuinely different same-source activities that
// happen to be close in time (e.g. two separate real rides) must never be
// force-merged just because they resemble each other on their own, but two
// real same-source rows that both corroborate against a shared third source
// safely can be.
function isLikelyDuplicate(a: Run, b: Run): boolean {
  if (a.date !== b.date) return false

  const typeA = canonicalActivityType(a.activityType)
  const typeB = canonicalActivityType(b.activityType)
  // Strava's own generic "Workout" activityType -- its catch-all for
  // activities it can't classify more specifically -- doesn't meaningfully
  // discriminate between families. Confirmed live: Hevy's own direct
  // Strava-posting integration uses this generic type for some workouts
  // (e.g. a recovery/mobility session) instead of "WeightTraining", which
  // would otherwise fail an exact-type match against the same workout's real
  // Hevy/Garmin rows before the date/time checks below ever got a chance to
  // confirm it's the same event. Treated as a wildcard here rather than
  // widening canonicalActivityType itself, which stays a general-purpose
  // classifier used elsewhere (e.g. the Activities page's type filter) where
  // this leniency wouldn't be appropriate.
  const isWildcard = (t: string) => t === "workout"
  if (typeA !== typeB && !isWildcard(typeA) && !isWildcard(typeB)) return false
  const effectiveType = isWildcard(typeA) ? typeB : typeA

  const toMin = (t: string) => {
    const [h, m] = t.split(":").map(Number)
    return h * 60 + m
  }

  // Strength activities carry no meaningful distance (Garmin/Hevy both report
  // 0 or null), so the distance-proximity check below can never confirm a
  // match for them -- match on start-time proximity alone instead, same
  // ~30min tolerance the backend's own Hevy<->Garmin matcher already uses
  // (garmin_enrich.py's find_matching_garmin_run) for exactly this pairing.
  if (effectiveType === "strength") {
    if (!a.startTime || !b.startTime) return false
    return Math.abs(toMin(a.startTime) - toMin(b.startTime)) <= 30
  }

  if (a.distanceMi == null || b.distanceMi == null) return false
  if (Math.abs(a.distanceMi - b.distanceMi) > Math.max(0.1, a.distanceMi * 0.05)) return false
  if (a.startTime && b.startTime) {
    if (Math.abs(toMin(a.startTime) - toMin(b.startTime)) > 10) return false
  }
  return true
}

// Strava preferred where it has data (better route/routeMetrics); Garmin fills in
// anything Strava lacks (mainly running-dynamics fields, which Strava never
// populates). Exception: Garmin's activity names are usually more descriptive than
// Strava's generic auto-names, so they win regardless of the general merge order.
// Hevy pairs (always strength, never involve Strava) follow a different rule:
// Hevy's manually-logged exercise/set data is authoritative over Garmin's own
// auto-detection (often a single unreliable "UNKNOWN" blob -- see
// stats._gear_kind_for_activity's own docstring on how unreliable that
// detection is), so Hevy's name/exerciseSets win regardless of general merge
// order, mirroring the Garmin-name exception above.
//
// Takes a group of 2+ mutual duplicates rather than just a pair -- a real
// account can have a Garmin activity auto-forwarded to Strava (a common Garmin
// Connect setting) on top of a Hevy log of the same workout, producing three
// (or more) raw rows for one physical session, not just two.
function mergeRunGroup(group: Run[]): Run {
  const strava = group.find((r) => r.source === "strava") ?? null
  const hevy = group.find((r) => r.source === "hevy") ?? null
  const garmin = group.find((r) => r.source === "garmin") ?? null
  const primary = strava ?? hevy ?? group[0]

  // Start from primary's own fields (never overwritten below), then let the
  // rest of the group backfill anything primary doesn't have -- equivalent to
  // the old two-way "secondary spread, then overlay primary's non-empty
  // fields" but generalized to any group size without needing a fold.
  const merged: Run = { ...primary }
  for (const r of group) {
    if (r === primary) continue
    Object.entries(r).forEach(([k, v]) => {
      if (isEmptyValue((merged as Record<string, unknown>)[k])) (merged as Record<string, unknown>)[k] = v
    })
  }
  if (hevy) {
    if (!isEmptyValue(hevy.name)) merged.name = hevy.name
    if (!isEmptyValue(hevy.exerciseSets)) merged.exerciseSets = hevy.exerciseSets
  } else if (garmin && !isEmptyValue(garmin.name)) {
    merged.name = garmin.name
  }
  // Garmin's route is parsed from the raw FIT record stream (see
  // garmin_sync.py's route_source diagnostic), not subject to whatever privacy
  // zone Strava applies -- confirmed live: this account has Strava's own
  // "hide first/last quarter mile" privacy setting on, which clips Strava's
  // route/routeMetrics but never Garmin's. Overrides the general primary-based
  // merge above (which would otherwise let Strava's route win whenever Strava
  // is present) whenever Garmin has real route data to offer.
  if (garmin && !isEmptyValue(garmin.route)) {
    merged.route = garmin.route
    merged.routeMetrics = garmin.routeMetrics
  }
  merged.mergedSources = Array.from(new Set(group.map((r) => r.source))).sort()
  merged.mergedIds = group.map((r) => r.id)
  return merged
}

export function mergeDuplicateRuns(rawRuns: Run[]): Run[] {
  const used = new Array(rawRuns.length).fill(false)
  const merged: Run[] = []
  for (let i = 0; i < rawRuns.length; i++) {
    if (used[i]) continue
    const group = [rawRuns[i]]
    used[i] = true
    // Re-scan to a fixed point rather than a single forward pass: a same-
    // source candidate that needs corroboration (see below) may sit earlier
    // in the array than the differently-sourced row that would corroborate
    // it, so one pass can miss it. Looping until a full pass adds nothing
    // new catches those regardless of array order.
    let addedAny = true
    while (addedAny) {
      addedAny = false
      for (let j = 0; j < rawRuns.length; j++) {
        if (used[j]) continue
        const candidate = rawRuns[j]
        const sameSourceAlreadyPresent = group.some((g) => g.source === candidate.source)
        if (sameSourceAlreadyPresent) {
          // Only join on third-party corroboration: a real account can have
          // BOTH Garmin->Strava and Hevy->Strava auto-posting independently for
          // one physical workout (confirmed live), producing two genuine Strava
          // rows for it. Matching against just one differently-sourced anchor
          // already in the group is enough evidence they're the same event --
          // requiring a match against every member would incorrectly demand the
          // two same-source rows resemble each other directly, which they often
          // won't (e.g. one carries real HR from the watch, the other doesn't).
          if (group.some((g) => g.source !== candidate.source && isLikelyDuplicate(g, candidate))) {
            group.push(candidate)
            used[j] = true
            addedAny = true
          }
        } else if (group.every((g) => isLikelyDuplicate(g, candidate))) {
          // No existing member shares this source -- require it to look like a
          // duplicate of EVERY current member, not just the first found, so a
          // loose match against one early item can't transitively pull in
          // something that doesn't actually belong with the rest of the group.
          group.push(candidate)
          used[j] = true
          addedAny = true
        }
      }
    }
    merged.push(group.length > 1 ? mergeRunGroup(group) : rawRuns[i])
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
