import { useQuery } from "@tanstack/react-query"
import { api } from "@/lib/api"
import { useAllRuns } from "./useActivities"
import { isRunActivity } from "@/lib/activities"

const ABSOLUTE_MIN_HR = 20
const FALLBACK_HR_FLOOR = 30

function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: api.config })
}

// Below this, it's a sensor dropout (dead HRM/optical sensor mid-activity), not a
// real reading — no human sustains single-digit or near-zero bpm during exercise.
// Raw values stay stored exactly as synced; this only gates chart/display use.
// Prefers a real measured resting HR from Garmin (floor = restingHR - 10%); falls
// back to the 5th percentile of valid avgHR readings minus 10% — the low end of
// real recorded exercise HR is always somewhat above true resting HR, so this is a
// safely conservative floor until Garmin wellness data is synced.
export function useHrFloor(): number {
  const { data: config } = useConfig()
  const { data: allRuns } = useAllRuns()

  if (config?.restingHrBpm) return Math.round(config.restingHrBpm * 0.9)

  if (!allRuns) return FALLBACK_HR_FLOOR
  const validHRs = allRuns
    .filter((r) => isRunActivity(r) && r.avgHR != null && r.avgHR >= ABSOLUTE_MIN_HR)
    .map((r) => r.avgHR as number)
    .sort((a, b) => a - b)
  if (!validHRs.length) return FALLBACK_HR_FLOOR
  const p5 = validHRs[Math.floor(validHRs.length * 0.05)]
  return Math.round(p5 * 0.9)
}

export function isPlausibleHR(bpm: number | null | undefined, hrFloor: number): boolean {
  return bpm != null && bpm >= hrFloor
}
