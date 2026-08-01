// Typed client over the existing FastAPI backend. Endpoint paths and response
// shapes are unchanged by the Phase 0 rewrite (see PLAN.md 0.1) — this file
// grows tab-by-tab as each port needs more endpoints, it does not attempt to
// cover the whole API up front.

export type { Run } from "./activities"
import type { Run } from "./activities"
import { getDemoSession, clearDemoSession } from "./demoAuth"

export interface HeaderStats {
  totalActivityCount: number
  runCountAllTime: number
  avgPaceSecPerMiAllTime: number | null
  weekMileageRun: number
}

export interface WeekMileage {
  weekStart: string
  totalMiles: number
  runCount: number
}

export interface MonthMileage {
  month: string
  totalMiles: number
  runCount: number
}

export interface TrainingLoad {
  last28DaysTSS: number
  prior28DaysTSS: number
  pctChange: number | null
  direction: "up" | "down" | "steady"
}

export interface ConsistencyStreak {
  streakWeeks: number
  minMiles: number
  minRuns: number | null
}

export interface DaysSinceRun {
  days: number
  date: string
  distanceMi?: number
  runId: string
  name?: string
}

export interface PersonalRecord {
  runId: string
  date: string
  name: string
  value: number
}

export interface PersonalRecords {
  longestRun: PersonalRecord | null
  fastestPace: PersonalRecord | null
  mostElevation: PersonalRecord | null
  longestDuration: PersonalRecord | null
}

export interface PaceTrendPoint {
  date: string
  paceSecPerMi: number
}

export interface DashboardSummary {
  weeklyMileage: WeekMileage[]
  trainingLoad: TrainingLoad
  consistencyStreak: ConsistencyStreak
  daysSinceLongestRun: DaysSinceRun | null
  daysSinceLastRun: DaysSinceRun | null
  paceTrend: PaceTrendPoint[]
  personalRecords: PersonalRecords
  monthlyMileage: MonthMileage[]
  headerStats: HeaderStats
}

export interface WellnessDay {
  date: string
  restingHrBpm: number | null
  vo2max: number | null
  sleepScore: number | null
  sleepSeconds: number | null
  deepSleepSeconds: number | null
  lightSleepSeconds: number | null
  remSleepSeconds: number | null
  awakeSleepSeconds: number | null
  racePredict5kSec: number | null
  racePredict10kSec: number | null
  racePredictHalfMarathonSec: number | null
  racePredictMarathonSec: number | null
  trainingReadinessScore: number | null
  trainingReadinessLevel: string | null
  // P10 — respiration, body battery, stress
  avgWakingRespirationRate: number | null
  avgSleepRespirationRate: number | null
  lowestRespirationRate: number | null
  highestRespirationRate: number | null
  bodyBatteryCharged: number | null
  bodyBatteryDrained: number | null
  avgStressLevel: number | null
  maxStressLevel: number | null
}

export interface DailyStepsPoint {
  date: string
  steps: number | null
}

// Phase 6.2 — PMC (fitness/fatigue/form). ctl="fitness" (42d rolling avg of daily
// training load), atl="fatigue" (7d rolling avg), tsb="form" (yesterday's ctl-atl).
export interface DailyMetricPoint {
  date: string
  dailyLoad: number
  ctl: number
  atl: number
  tsb: number
}

export interface GeocodeResult {
  label: string
  cached: boolean
}

export interface ToolCall {
  tool: string
  input: Record<string, unknown>
}

export interface ChartSpec {
  chartType: "line" | "bar"
  title: string
  labels: string[]
  datasets: { label: string; data: number[] }[]
}

export interface ChatMessage {
  role: "user" | "assistant"
  content: string
  toolCalls: ToolCall[] | null
  charts: ChartSpec[] | null
  createdAt?: string
}

export interface ChatStatus {
  configured: boolean
}

export type CoachPersonality = "encouraging" | "normal" | "spicy" | "insulting"

// Phase 12.5 — one rolling draft GitHub issue per user, accumulated from the
// periodic self-review job and the live log_product_feedback chat tool. Draft-only,
// never auto-posted to github.com — see Settings' Coach Feedback section.
export interface CoachIssueDraft {
  title: string
  body: string
  frustrationCount: number
  updatedAt: string
}

// Deliberately never throws — mirrors the legacy send() closure's distinction
// between an HTTP error (server responded, has a `detail` message) and a
// network/fetch failure (no response at all), which get different display text.
// `retryable` says whether re-sending could plausibly succeed; the caller uses it
// to decide between offering a Retry affordance and recording a permanent error
// turn in the transcript. This send is never retried automatically — see ChatPage.
export type ChatSendResult =
  | { ok: true; reply: string; toolCalls: ToolCall[]; charts: ChartSpec[] }
  | { ok: false; kind: "http" | "network"; message: string; retryable: boolean }

export interface SleepStageSegment {
  stage: string
  start: string
  end: string
}

export interface SleepStagesResponse {
  availableDates: string[]
  date: string | null
  segments: SleepStageSegment[]
}

export type GoalType = "race" | "consistency" | "distance_target"
export type GoalStatus = "active" | "completed" | "abandoned"

export interface LinkedRun {
  runId: string
  name: string
  date: string
  distanceMi: number | null
  movingTimeSec: number | null
  avgPaceSecPerMi: number | null
}

export interface GoalProgress {
  goalType: GoalType
  // race
  daysUntil?: number | null
  recent28DayMiles?: number
  recent28DayRunCount?: number
  linkedRun?: LinkedRun
  // consistency
  streakWeeks?: number
  currentWeekMiles?: number
  currentWeekRunCount?: number
  // distance_target
  completedMi?: number
  pctComplete?: number | null
  daysRemaining?: number | null
}

// P20 shipped one TrainingPlan per goal. P21 (docs/P21_CONCURRENT_GOALS.md) redesigned
// this to ONE active plan per user, serving multiple goals via PlanGoalRef in a role —
// "realistically the user can only do one training and the plans should intertwine."
// The plan's primary goal now steers what the nightly generator actually prescribes
// (generator.resolve_periodization_goal); supporting goals get their own honest
// projection of what they alone would need, not a claim about what's really scheduled.
export interface PlanGoalRef {
  goalId: string
  goalName: string | null
  goalTargetDate: string | null
  role: "primary" | "supporting"
  // False means this goal isn't the one the generator is currently periodizing for —
  // true only for the plan's primary goal while it's still a live active race. A
  // supporting goal's week series describes its own arc, not what's actually prescribed.
  isActivePeriodizationGoal: boolean
}

// Hours demand vs. the declared cap. Covers HALE's OWN prescribed sessions only —
// Garmin and the coach also write sessions for the same days and the athlete does one
// of them, so summing every source would roughly triple the figure. The UI must say so.
export interface PlanHours {
  // Per planner, never summed: Garmin, the coach and HALE all write sessions for the
  // same days and you do one of them, so a combined total would roughly triple it.
  bySource: Record<string, { hours: number; unknownSessions: number }>
  longestSessionHours: number | null
  // Cap comparison uses HALE's own plan — the only week this app is responsible for.
  // All null when no cap is set: nothing to be over, and none is invented.
  capHours: number | null
  demandHours: number | null
  unknownSessions: number
  overBy: number | null
  isOverCap: boolean
}

// One weekday's availability. freeHours is real arithmetic (awake window minus declared
// work); suggestedCapHours is an explicit heuristic about work/life balance, which is
// why it can be overridden. Nulls mean "not enough data", never a guess.
export interface DayTimeBudget {
  weekday: number // 0=Mon
  wakeTime: string | null
  bedTime: string | null
  workStart: string | null
  workEnd: string | null
  workHours: number | null
  freeHours: number | null
  suggestedCapHours: number | null
  capHours: number | null
  isOverridden: boolean
}

export interface TrainingPlan {
  id: string
  status: "active" | "archived"
  createdAt: string
  weeklyHoursCap: number | null // null = no cap set; the user's own call, never fabricated
  availableDays: number[] | null // 0=Mon..6=Sun; null = every day available
  longRunDay: number | null // 0=Mon..6=Sun; null = today's hardcoded skeleton default
  sleepConstraintMode: "soft" | "off"
  hours: PlanHours | null
  typicalWakeTime: string | null // "HH:MM" local; null until enough real nights exist
  timeBudget: DayTimeBudget[]
  garminPlan: GarminPlanFreshness
  goals: PlanGoalRef[]
}

// Garmin revises the same day's suggestion repeatedly, so a stale copy genuinely
// disagrees with the Garmin app. Shown rather than presented as current.
export interface GarminPlanFreshness {
  lastCheckedAt: string | null
  ageHours: number | null
  isStale: boolean
  inCooldown: boolean
  cooldownUntil: string | null
  consecutiveFailures: number
}

export interface PlanWeek {
  weekStart: string
  phase: "base" | "build" | "peak" | "taper"
  isDeload: boolean
  frozen: boolean
  targetMi: number
  actualMi: number | null // null for a week that hasn't happened yet
  isProjection: boolean
  isPersisted: boolean
  isCurrentWeek: boolean
}

export interface PlanWeeksResponse {
  planId: string
  goalId: string
  goalName: string | null
  goalTargetDate: string | null
  role: "primary" | "supporting"
  isActivePeriodizationGoal: boolean
  weeks: PlanWeek[]
  // Trailing 7 days including today. Not the same as the current week's actualMi — on a
  // Monday that one is legitimately 0.0 while this reflects a full week of training.
  last7DaysMi: number
}

export type GearKind = "shoe" | "bike" | "bike_component"

export interface Gear {
  id: string
  name: string
  kind: GearKind
  parentGearId: string | null
  isDefault: boolean
  startDate: string | null
  retiredDate: string | null
  replaceAtMi: number | null
  notes: string | null
  createdAt: string
  totalMiles: number
  wearPct: number | null
}

export interface GearInput {
  name: string
  kind: GearKind
  parentGearId?: string | null
  isDefault?: boolean
  startDate?: string
  retiredDate?: string | null
  replaceAtMi?: number | null
  notes?: string | null
}

export interface Goal {
  id: string
  goalType: GoalType
  name: string
  status: GoalStatus
  activityTypes: string[]
  targetValue: number | null
  targetUnit: string | null
  targetDate: string | null
  startDate: string | null
  notes: string
  priority: number
  createdAt: string
  completedAt: string | null
  progress: GoalProgress
}

// Only the fields relevant to the currently-selected goalType are ever sent —
// matches legacy's openGoalModal() save handler exactly, which builds this
// object with an if/else per type rather than always including every field
// (e.g. editing a race goal into a consistency goal never re-sends targetDate,
// so a stale value harmlessly persists server-side but is never read again,
// since goal_progress()'s dispatch is entirely keyed on goal_type).
export interface GoalInput {
  goalType: GoalType
  name: string
  activityTypes: string[]
  notes: string
  priority: number
  targetValue?: number | null
  targetUnit?: string | null
  targetDate?: string | null
  startDate?: string | null
}

export type WorkoutType = "easy" | "tempo" | "interval" | "long" | "rest" | "strength" | "cross_train"
export type WorkoutStatus = "planned" | "completed" | "skipped" | "modified"

// The original shape — still used by any step with no `stepType` (every
// already-scheduled mobility/warmup workout keeps rendering/editing exactly as before).
export interface LegacyStep {
  stepType?: undefined
  exercise: string
  side: string | null
  durationSec: number | null
  reps: number | null
  notes: string | null
  howTo: string | null
}

export type EnduranceStepType = "warmup" | "active" | "rest" | "cooldown" | "repeat"
export type TargetType = "hr_zone" | "hr_custom" | "power" | "pace" | "cadence" | "open"

// Phase 4.2 — structured endurance steps. Metric units (distanceM in meters).
export interface EnduranceStep {
  stepType: EnduranceStepType
  durationSec: number | null
  distanceM: number | null
  targetType: TargetType
  targetZone: number | null
  targetLow: number | null
  targetHigh: number | null
  repeatCount?: number
  children?: WorkoutStep[]
}

export type SetTargetType = "reps" | "hold_sec"

export interface StrengthSet {
  index: number
  targetType: SetTargetType
  targetReps: number | null
  targetHoldSec: number | null
  targetWeightLb: number | null
  actualReps: number | null
  actualHoldSec: number | null
  actualWeightLb: number | null
  completedAt: string | null
}

// Phase 4.4 — restSeconds lives on the exercise (not per-set), mirroring the real
// Hevy routine shape this was modeled on.
export interface StrengthStep {
  stepType: "strength_exercise"
  exercise: string
  restSeconds: number
  sets: StrengthSet[]
}

export type WorkoutStep = LegacyStep | EnduranceStep | StrengthStep

// Present only on sessions prescribed by heart rate + duration with no distance —
// Garmin's adaptive plan works that way, so those days would otherwise show no distance
// at all. Derived at read time from the user's own recent runs at a similar HR doing the
// same KIND of session; never stored, because it depends on current fitness.
export interface EstimatedDistance {
  distanceMi: number
  paceSecPerMi: number
  basePaceSecPerMi: number
  heatMultiplier: number
  heatIndexF: number | null
  targetHr: number
  durationSec: number
  sampleSize: number
  hrBandBpm: number
  // False means there weren't enough same-kind sessions and the estimate fell back to
  // pooling all run types — materially less reliable, so the UI should say so.
  typeMatched: boolean
}

export interface Workout {
  id: string
  scheduledDate: string
  workoutType: WorkoutType
  activityType: string
  targetDistanceMi: number | null
  targetPaceSecPerMi: number | null
  targetDurationSec: number | null
  targetHrBpm: number | null
  notes: string | null
  steps: WorkoutStep[] | null
  status: WorkoutStatus
  linkedRunId: string | null
  critiqueText: string | null
  createdAt: string
  source: string
  estimatedDistance?: EstimatedDistance
}

export interface WorkoutInput {
  scheduledDate: string
  workoutType: WorkoutType
  activityType: string | null
  targetDistanceMi: number | null
  targetPaceSecPerMi: number | null
  targetDurationSec: number | null
  notes: string
  steps?: WorkoutStep[] | null
}

export interface TrainingConfig {
  maxHr: number | null
  thresholdHr: number | null
  ftpWatts: number | null
  zones: Record<string, [number, number]> | null
  weeklyRampPct: number
  mesocyclePattern: string
  distribution: string
  strengthDaysPerWeek: number
  strengthTemplate: string
  // 0 = off. When set, the weekly rotation's cross-train slot becomes a real prescribed
  // ride instead of an empty placeholder. Only 1/week is meaningfully supported today.
  rideDaysPerWeek: number
}

export interface RecoveryTool {
  id: string
  name: string
  category: string
  minLevel: number
  maxLevel: number
  minDurationMin: number
  maxDurationMin: number
  durationIncrementMin: number
  supportsZoneBoost: boolean
  notes: string | null
}

export type RecoverySessionStatus = "planned" | "completed" | "skipped"

export interface RecoverySession {
  id: string
  toolId: string
  scheduledDate: string
  level: number
  durationMin: number
  zoneBoost: boolean
  rationale: string | null
  status: RecoverySessionStatus
  createdAt: string
}

// Phase 14 — Quick Generate (via NewWorkoutDialog). "run"/"ride" resolve to a
// Workout in the response, "recovery" to a RecoverySession — useQuickGenerate
// just invalidates both query keys after a real (non-dry-run) call rather than
// branching on the shape.
export type QuickGenerateDomain = "run" | "ride" | "strength" | "recovery"

export interface QuickGenerateResult {
  date: string
  domain: QuickGenerateDomain
  result: Workout | RecoverySession
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

// Is this failure worth trying again, or will it fail identically forever?
// A thrown non-ApiError means fetch() itself rejected (offline, DNS, connection
// refused, TLS) — no response was ever received, so it's transient by definition.
// An ApiError carries a real status: only 5xx (server-side, often a restart or a
// blip) and 408/429 (explicitly "try again") can change on their own. Every other
// 4xx is a statement about the request, and retrying it just burns time — notably
// 401, which Phase 11's demo-session interceptor is concurrently redirecting on.
export function isTransientError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status >= 500 || error.status === 408 || error.status === 429
  }
  return true
}

// Attaches the demo session's token (if one exists — a no-op on the real NAS
// deployment, which never has one) so a demo visitor authenticates on every call.
function demoAuthHeader(): Record<string, string> {
  const session = getDemoSession()
  return session ? { "X-Api-Token": session.token } : {}
}

// A 401 only ever means "the demo session is gone" (expired, swept, or logged out
// elsewhere) — the real NAS deployment never sends a token in the first place, so it
// never gets a 401 to begin with. Hard redirect (not client-side navigation) so
// DemoGate re-evaluates from scratch against the now-cleared session.
function handleUnauthorized(res: Response) {
  if (res.status === 401 && getDemoSession()) {
    clearDemoSession()
    window.location.href = "/demo-login"
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...demoAuthHeader() },
    ...init,
  })
  handleUnauthorized(res)
  if (!res.ok) {
    throw new ApiError(res.status, `${init?.method ?? "GET"} ${path} failed: ${res.status}`)
  }
  return res.json() as Promise<T>
}

export interface Config {
  syncIntervalHours: number
  syncActivityLimit: number
  restingHrBpm: number | null
  pushConfigured: boolean
  isDemoUser: boolean
  timezone: string
}

export interface DemoStatus {
  enabled: boolean
}

// Body-metric profile fields — separate from Config (app-level settings).
// Currently just the four fields Phase 9.5's planned BMR estimate needs;
// nothing reads these yet, but Settings lets the user fill them in ahead of time.
export type Sex = "male" | "female" | "other"

export interface Profile {
  heightIn: number | null
  weightLb: number | null
  dateOfBirth: string | null
  sex: Sex | null
}

export interface DemoSessionResponse {
  token: string
  userId: string
  expiresAt: string
}

export interface PushVapidKey {
  configured: boolean
  publicKey: string | null
}

export interface StravaStatus {
  connected: boolean
}

export interface GarminStatus {
  configured: boolean
}

export interface HevyStatus {
  configured: boolean
}

export type HevyConnectionResult = { ok: true } | { ok: false; message: string }

export interface SyncMetaInfo {
  lastSyncedAt: string | null
  lastCount: number | null
  lastError: string | null
}

// P8 — Garmin-only additive fields so the UI can explain *why* data looks stale
// (auto-poll off, still cooling down after a rate limit) instead of a bare timestamp.
export interface GarminSyncMetaInfo extends SyncMetaInfo {
  autoPollEnabled: boolean
  lastAutoPollAttemptAt: string | null
  cooldownUntil: string | null
  consecutiveFailures: number
}

export interface SyncMeta {
  strava: SyncMetaInfo
  garmin: GarminSyncMetaInfo
  hevy: SyncMetaInfo
}

export interface Connection {
  provider: string
  username: string
}

export interface RouteDiagnostics {
  fit_record_stream: number
  geopolyline_summary: number
  none: number
  unknown: number
}

export type SyncSource = "strava" | "garmin" | "hevy"

export interface SyncJob {
  status: "idle" | "running" | "done" | "error"
  count: number
  log: string[]
  startedAt: string | null
  finishedAt: string | null
  error: string | null
}

export interface BacklogJob extends SyncJob {
  lastCompleted: { syncedAt: string | null; count: number | null; error: string | null }
}

export interface GarminImportSummary {
  filesScanned: number
  jsonFilesParsed: number
  fitFilesFound: number
  activityRecordsFound: number
  activitiesImported: number
  activitiesSkippedExisting: number
  activitiesSkippedMalformed: number
  dailyWellnessRecordsFound: number
  dailyStepsImported: number
  errors: string[]
}

// These three mirror legacy's per-button fetch handlers, which read `data.detail`
// on a non-OK response — request<T>() (below) doesn't expose the response body
// on failure, so these bypass it and never throw, matching the button-disables-
// then-shows-an-inline-message UX exactly.
export type SyncStartResult = { ok: true } | { ok: false; message: string }
export type GarminImportResult = { ok: true; summary: GarminImportSummary } | { ok: false; message: string }

export interface RunsQuery {
  start?: string
  end?: string
  all?: boolean
}

export interface RunUpdate {
  type?: string
  tempF?: number | null
  weatherCondition?: string | null
  rpe?: number | null
  isTreadmill?: boolean
  notes?: string
  gearId?: string | null
}

export interface ApiTokenSummary {
  id: string
  name: string | null
  createdAt: string
  lastUsedAt: string | null
}

// Only the create response ever carries the raw token — the server persists just
// its SHA-256 hash, so this is the one and only chance to see/copy it.
export interface ApiTokenCreated extends ApiTokenSummary {
  token: string
}

export const api = {
  dashboardSummary: () => request<DashboardSummary>("/api/dashboard/summary"),
  config: () => request<Config>("/api/config"),
  updateConfig: (body: { timezone: string }) =>
    request<{ timezone: string }>("/api/config", { method: "PATCH", body: JSON.stringify(body) }),
  profile: () => request<Profile>("/api/profile"),
  updateProfile: (body: Partial<Profile>) =>
    request<Profile>("/api/profile", { method: "PATCH", body: JSON.stringify(body) }),
  goals: () => request<Goal[]>("/api/goals"),
  createGoal: (body: GoalInput) => request<Goal>("/api/goals", { method: "POST", body: JSON.stringify(body) }),
  updateGoal: (id: string, body: Partial<GoalInput> & { status?: GoalStatus }) =>
    request<Goal>(`/api/goals/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteGoal: (id: string) => request<{ deleted: true }>(`/api/goals/${id}`, { method: "DELETE" }),
  // Returns the user's single active plan, or null (see TrainingPlan's P21 docstring).
  plans: () => request<TrainingPlan | null>("/api/plans"),
  // Attaches goalId to the user's plan (creating it on first call). role omitted defaults
  // to "primary" for the plan's first goal, "supporting" thereafter — pass it explicitly
  // to add a supporting goal or to reassign which goal is primary.
  addPlanGoal: (goalId: string, role?: "primary" | "supporting") =>
    request<TrainingPlan>("/api/plans", { method: "POST", body: JSON.stringify({ goalId, role }) }),
  // Only the keys present in `body` are applied — omitting one leaves it untouched,
  // while sending it explicitly as null clears it. null is meaningful for all four
  // (it means "not declared"), so the two cases can't be collapsed.
  updatePlan: (
    planId: string,
    body: Partial<{
      weeklyHoursCap: number | null
      availableDays: number[] | null
      longRunDay: number | null
      sleepConstraintMode: "soft" | "off"
      workSchedule: Record<string, { start: string; end: string; breakHours?: number }> | null
      dailyTrainingCaps: Record<string, number> | null
    }>,
  ) => request<TrainingPlan>(`/api/plans/${planId}`, { method: "PATCH", body: JSON.stringify(body) }),
  planWeeks: (planId: string, goalId: string, opts?: { weeksBack?: number; weeksForward?: number }) => {
    const params = new URLSearchParams({ goalId })
    if (opts?.weeksBack != null) params.set("weeksBack", String(opts.weeksBack))
    if (opts?.weeksForward != null) params.set("weeksForward", String(opts.weeksForward))
    return request<PlanWeeksResponse>(`/api/plans/${planId}/weeks?${params.toString()}`)
  },

  gear: () => request<Gear[]>("/api/gear"),
  createGear: (body: GearInput) => request<Gear[]>("/api/gear", { method: "POST", body: JSON.stringify(body) }),
  updateGear: (id: string, body: Partial<GearInput>) =>
    request<Gear[]>(`/api/gear/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteGear: (id: string) => request<{ deleted: true }>(`/api/gear/${id}`, { method: "DELETE" }),

  stravaStatus: () => request<StravaStatus>("/api/strava/status"),
  garminStatus: () => request<GarminStatus>("/api/garmin/status"),
  hevyStatus: () => request<HevyStatus>("/api/hevy/status"),
  syncMeta: () => request<SyncMeta>("/api/sync/meta"),
  connections: () => request<Connection[]>("/api/connections"),
  routeDiagnostics: () => request<RouteDiagnostics>("/api/garmin/route-diagnostics"),
  syncStatus: (source: SyncSource) => request<SyncJob>(`/api/sync/${source}/status`),
  backlogStatus: (source: SyncSource) => request<BacklogJob>(`/api/sync/${source}/backlog/status`),
  saveGarminConnection: (username: string, password: string) =>
    request<{ status: string }>("/api/connections/garmin", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  deleteConnection: (provider: string) =>
    request<{ deleted: boolean }>(`/api/connections/${provider}`, { method: "DELETE" }),
  // Bypasses request<T>() (see SyncStartResult above) so a rejected API key's real
  // "check your key / requires Hevy Pro" message reaches the user instead of a
  // bare "POST .../hevy failed: 400".
  saveHevyConnection: async (apiKey: string): Promise<HevyConnectionResult> => {
    try {
      const res = await fetch("/api/connections/hevy", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...demoAuthHeader() },
        body: JSON.stringify({ apiKey }),
      })
      handleUnauthorized(res)
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        return { ok: false, message: data.detail || "Failed to save Hevy connection" }
      }
      return { ok: true }
    } catch {
      return { ok: false, message: "Failed to save Hevy connection" }
    }
  },
  setCoachPersonality: (personality: CoachPersonality) =>
    request<{ personality: CoachPersonality }>("/api/coach/personality", {
      method: "POST",
      body: JSON.stringify({ personality }),
    }),
  manualSync: async (source: SyncSource): Promise<SyncStartResult> => {
    try {
      const res = await fetch(`/api/sync/${source}`, { method: "POST", headers: demoAuthHeader() })
      handleUnauthorized(res)
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        return { ok: false, message: data.detail || "Failed to start sync" }
      }
      return { ok: true }
    } catch {
      return { ok: false, message: "Failed to start sync" }
    }
  },
  backlogSync: async (source: SyncSource): Promise<SyncStartResult> => {
    try {
      const res = await fetch(`/api/sync/${source}/backlog`, { method: "POST", headers: demoAuthHeader() })
      handleUnauthorized(res)
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        return { ok: false, message: data.detail || "Failed to start backlog sync" }
      }
      return { ok: true }
    } catch {
      return { ok: false, message: "Failed to start backlog sync" }
    }
  },
  garminImport: async (file: File): Promise<GarminImportResult> => {
    try {
      const formData = new FormData()
      formData.append("file", file)
      const res = await fetch("/api/garmin/import", { method: "POST", body: formData, headers: demoAuthHeader() })
      handleUnauthorized(res)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) return { ok: false, message: data.detail || "Import failed" }
      return { ok: true, summary: data }
    } catch (e) {
      return { ok: false, message: `Import failed: ${String(e)}` }
    }
  },
  tokens: () => request<ApiTokenSummary[]>("/api/tokens"),
  createToken: (name: string) =>
    request<ApiTokenCreated>("/api/tokens", { method: "POST", body: JSON.stringify({ name }) }),
  deleteToken: (id: string) => request<{ deleted: true }>(`/api/tokens/${id}`, { method: "DELETE" }),
  runs: (query: RunsQuery = {}) => {
    const params = new URLSearchParams()
    if (query.all) params.set("all", "true")
    if (query.start) params.set("start", query.start)
    if (query.end) params.set("end", query.end)
    const qs = params.toString()
    return request<Run[]>(`/api/runs${qs ? `?${qs}` : ""}`)
  },
  wellness: (query: RunsQuery & { days?: number } = {}) => {
    const params = new URLSearchParams()
    if (query.all) params.set("all", "true")
    if (query.start) params.set("start", query.start)
    if (query.end) params.set("end", query.end)
    if (query.days != null) params.set("days", String(query.days))
    const qs = params.toString()
    return request<WellnessDay[]>(`/api/wellness${qs ? `?${qs}` : ""}`)
  },
  metrics: (query: RunsQuery & { days?: number } = {}) => {
    const params = new URLSearchParams()
    if (query.all) params.set("all", "true")
    if (query.start) params.set("start", query.start)
    if (query.end) params.set("end", query.end)
    if (query.days != null) params.set("days", String(query.days))
    const qs = params.toString()
    return request<DailyMetricPoint[]>(`/api/metrics${qs ? `?${qs}` : ""}`)
  },
  steps: (query: RunsQuery & { days?: number } = {}) => {
    const params = new URLSearchParams()
    if (query.all) params.set("all", "true")
    if (query.start) params.set("start", query.start)
    if (query.end) params.set("end", query.end)
    if (query.days != null) params.set("days", String(query.days))
    const qs = params.toString()
    return request<DailyStepsPoint[]>(`/api/steps${qs ? `?${qs}` : ""}`)
  },
  geocode: (lat: number, lon: number) => request<GeocodeResult>(`/api/geocode?lat=${lat}&lon=${lon}`),

  chatStatus: () => request<ChatStatus>("/api/chat/status"),
  coachPersonality: () => request<{ personality: CoachPersonality }>("/api/coach/personality"),
  dailyCoachReport: () => request<{ report: string | null; date: string }>("/api/coach/daily-report"),
  chatHistory: () => request<ChatMessage[]>("/api/chat/history"),
  resetChat: () => request<{ status: string }>("/api/chat/reset", { method: "POST" }),
  sendChatMessage: async (message: string, activityId?: string): Promise<ChatSendResult> => {
    try {
      const res = await fetch("/api/chat/message", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...demoAuthHeader() },
        body: JSON.stringify({ message, ...(activityId ? { activityId } : {}) }),
      })
      handleUnauthorized(res)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        return {
          ok: false,
          kind: "http",
          message: data.detail || "something went wrong",
          retryable: isTransientError(new ApiError(res.status, "")),
        }
      }
      return { ok: true, reply: data.reply, toolCalls: data.toolCalls ?? [], charts: data.charts ?? [] }
    } catch {
      // fetch() rejected outright, so no response came back. Retryable, but only
      // on an explicit user action: the server may still have processed this
      // message, and its tool calls can write HealthNote/Workout rows.
      return { ok: false, kind: "network", message: "Network error — check your connection.", retryable: true }
    }
  },
  sleepStages: (date?: string) =>
    request<SleepStagesResponse>(`/api/wellness/sleep-stages${date ? `?date=${date}` : ""}`),
  coachIssue: () => request<CoachIssueDraft | null>("/api/coach-issue"),
  refreshCoachIssue: () => request<CoachIssueDraft | null>("/api/coach-issue/refresh", { method: "POST" }),
  clearCoachIssue: () => request<{ cleared: true }>("/api/coach-issue/clear", { method: "POST" }),
  updateRun: (id: string, body: RunUpdate) =>
    request<Run>(`/api/runs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  workouts: () => request<Workout[]>("/api/workouts"),
  createWorkout: (body: WorkoutInput) =>
    request<Workout>("/api/workouts", { method: "POST", body: JSON.stringify(body) }),
  updateWorkout: (id: string, body: Partial<WorkoutInput & { status: WorkoutStatus }>) =>
    request<Workout>(`/api/workouts/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteWorkout: (id: string) => request<{ deleted: true }>(`/api/workouts/${id}`, { method: "DELETE" }),

  trainingConfig: () => request<TrainingConfig>("/api/training-config"),
  updateTrainingConfig: (body: Partial<TrainingConfig>) =>
    request<TrainingConfig>("/api/training-config", { method: "PATCH", body: JSON.stringify(body) }),

  quickGenerate: (domain: QuickGenerateDomain, templateOverride?: string, dryRun?: boolean) => {
    const params = new URLSearchParams()
    if (templateOverride) params.set("template_override", templateOverride)
    if (dryRun) params.set("dry_run", "true")
    const qs = params.toString()
    return request<QuickGenerateResult>(`/api/generator/quick/${domain}${qs ? `?${qs}` : ""}`, { method: "POST" })
  },

  recoveryTools: () => request<RecoveryTool[]>("/api/recovery-tools"),
  recoverySessions: () => request<RecoverySession[]>("/api/recovery-sessions"),
  updateRecoverySessionStatus: (id: string, status: RecoverySessionStatus) =>
    request<RecoverySession>(`/api/recovery-sessions/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  deleteRecoverySession: (id: string) =>
    request<{ deleted: true }>(`/api/recovery-sessions/${id}`, { method: "DELETE" }),

  pushVapidKey: () => request<PushVapidKey>("/api/push/vapid-public-key"),
  pushSubscribe: (subscription: PushSubscriptionJSON) =>
    request<{ subscribed: true }>("/api/push/subscribe", { method: "POST", body: JSON.stringify(subscription) }),
  pushUnsubscribe: (endpoint: string) =>
    request<{ unsubscribed: true }>("/api/push/unsubscribe", {
      method: "POST",
      body: JSON.stringify({ endpoint }),
    }),
  pushTest: () => request<{ sent: number }>("/api/push/test", { method: "POST" }),

  // Deliberately a plain, separate fetch — never routed through request()/its
  // 401-interceptor. This is called before any demo session may exist (it's what
  // DemoGate uses to decide whether to gate at all), so coupling it to the same
  // token-clear-and-redirect logic risks a redirect loop on a transient hiccup.
  demoStatus: async (): Promise<DemoStatus> => {
    const res = await fetch("/auth/demo/status")
    if (!res.ok) throw new ApiError(res.status, `GET /auth/demo/status failed: ${res.status}`)
    return res.json()
  },
  demoLogin: () =>
    request<DemoSessionResponse>("/auth/demo/login", {
      method: "POST",
      body: JSON.stringify({ username: "demo", password: "demo" }),
    }),
  demoLogout: () => request<{ loggedOut: true }>("/auth/demo/logout", { method: "POST" }),
}
