// Grade-Adjusted Pace (Minetti cost-of-running model) — deliberately duplicated
// client-side so per-split GAP can redraw without a round trip. This is the one
// documented exception to "compute once at sync time" (see CLAUDE.md's
// Architecture section): app/util.py has the authoritative server-side
// implementation; this must be kept in sync by hand if the formula ever changes.
export function minettiCost(i: number): number {
  const i2 = i * i
  const i3 = i2 * i
  const i4 = i3 * i
  const i5 = i4 * i
  return 155.4 * i5 - 30.4 * i4 - 43.3 * i3 + 46.3 * i2 + 19.5 * i + 3.6
}

export function gapSecPerMi(
  pace: number | null | undefined,
  elevFt: number | null | undefined,
  distMi: number | null | undefined,
): number | null {
  if (!pace || elevFt == null || !distMi) return null
  const grade = Math.max(-0.3, Math.min(0.3, elevFt / 5280 / distMi))
  return pace / (minettiCost(grade) / minettiCost(0))
}

// Whole-run GAP from real per-point route data (RunCard's overall badge) — a
// meaningfully more accurate alternative to gapSecPerMi() above, which infers
// one average "grade" for the entire run from total elevation *gain* divided
// by distance. That's a gain-only proxy with no descent subtracted, so any
// run with real descents (i.e. almost every outdoor run that isn't a one-way
// climb) reads as more uphill than it actually was on net, understating true
// pace and overstating the correction — in one real comparison this
// overstated GAP by ~15s/mi against Garmin's own figure for the same run.
//
// Each routeMetrics point already carries its own real signed grade (from
// the raw altitude+distance stream, see strava.py/garmin_sync.py's route
// point builder) and its own instantaneous pace, so this averages those
// per-point grade-adjusted results directly instead of collapsing the whole
// run to one grade number first.
//
// Critically, this averages *speed*, not pace: points are captured at
// roughly uniform time intervals, not uniform distance, so slower stretches
// (hills, corners) produce more samples per mile than faster ones — a plain
// average of per-point *pace* over-weights those slow stretches (verified:
// ~9:31/mi average pace here vs. the run's real 9:15/mi average). Averaging
// adjusted *speed* first and inverting back to pace at the end is the
// correct time-weighted average — confirmed against the real discrepancy
// this was reported against: 9:14/mi computed this way vs. Garmin's 9:13/mi
// for the same run, essentially exact; the old method gave 8:58/mi.
//
// No per-split equivalent yet: routeMetrics points don't carry a cumulative
// distance, only lat/lon, so there's no reliable way to bucket points into
// mile splits client-side without a backend change (and re-syncing every
// past run) — SplitsTable's per-split GAP still uses the same gain-only
// shortcut as gapSecPerMi() and carries a smaller version of this bias.
const MIN_PLAUSIBLE_PACE_SEC = 240
const MAX_PLAUSIBLE_PACE_SEC = 2400

export function avgGapFromRoutePoints(
  points: { paceSecPerMi: number | null; gradePct: number | null }[] | null | undefined,
): number | null {
  if (!points) return null
  let totalAdjSpeed = 0
  let n = 0
  for (const p of points) {
    if (!p.paceSecPerMi || p.gradePct == null) continue
    if (p.paceSecPerMi < MIN_PLAUSIBLE_PACE_SEC || p.paceSecPerMi > MAX_PLAUSIBLE_PACE_SEC) continue
    const grade = Math.max(-0.3, Math.min(0.3, p.gradePct / 100))
    const factor = minettiCost(grade) / minettiCost(0)
    totalAdjSpeed += (1 / p.paceSecPerMi) * factor
    n++
  }
  if (n < 2) return null
  return 1 / (totalAdjSpeed / n)
}
