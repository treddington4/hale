// Formatting helpers ported from app/static/app.js — kept as plain functions
// (not locale/Intl-driven) to match the legacy app's exact display strings.

export function paceStr(sec: number | null | undefined): string {
  if (!sec || !isFinite(sec)) return "--:--"
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}:${String(s).padStart(2, "0")}`
}

export function timeStr(sec: number | null | undefined): string {
  if (!sec) return "--"
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.round(sec % 60)
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`
}

// Real race distances (miles) — for converting a total race-time prediction into an
// equivalent min/mile pace, e.g. for the Home page's Race Predictions card.
export const RACE_DISTANCE_MI = {
  "5k": 3.10686,
  "10k": 6.21371,
  half: 13.10938,
  marathon: 26.21875,
} as const

export function racePaceStr(raceTimeSec: number | null | undefined, distanceMi: number): string {
  if (!raceTimeSec) return "--:--/mi"
  return `${paceStr(raceTimeSec / distanceMi)}/mi`
}

export function fmtPctChange(pct: number | null | undefined): string {
  if (pct == null) return "--"
  return `${pct > 0 ? "+" : ""}${pct}%`
}

export function fmtSleepDuration(seconds: number | null | undefined): string | null {
  if (!seconds) return null
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

// Local calendar date (not toISOString()'s UTC date) — near midnight UTC (e.g.
// evening in US timezones) the two disagree, which would otherwise misplace
// today's items by a few hours. Matches app.js's toDateInputValue(todayMidnight()).
export function todayLocalDateString(): string {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, "0")
  const day = String(d.getDate()).padStart(2, "0")
  return `${y}-${m}-${day}`
}

export function tempColor(f: number | null | undefined): string {
  if (f == null) return "#5A6270"
  const clamp = Math.max(30, Math.min(100, f))
  const pct = (clamp - 30) / 70
  const cold = [76, 201, 240]
  const hot = [255, 107, 53]
  const c = cold.map((c0, i) => Math.round(c0 + (hot[i] - c0) * pct))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}

export function fmtGoalDate(d: string | null | undefined): string {
  if (!d) return ""
  return new Date(d + "T00:00:00").toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })
}

// A distance-sensor glitch (near-zero distance over real elapsed time) produces a
// nonsense pace when divided out — guards both ends of a sane human-running range.
export function isPlausiblePace(paceSecPerMi: number | null | undefined, distanceMi: number | null | undefined) {
  if (paceSecPerMi == null) return false
  if (distanceMi != null && distanceMi < 0.1) return false
  return paceSecPerMi >= 240 && paceSecPerMi <= 2400
}
