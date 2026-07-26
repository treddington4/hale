import { useEffect } from "react"
import { useNavigate, useLocation } from "react-router-dom"
import { useStravaStatus, useGarminStatus } from "@/hooks/useSettings"
import { useGoals } from "@/hooks/useGoals"
import { useAllRuns } from "@/hooks/useActivities"

// Redirects to /onboarding only for a genuinely fresh account — no Strava connected,
// no Garmin configured, zero goals, zero runs ever synced. Deliberately conservative:
// this must never fire for an already-populated account (like the one this app has
// been developed against all along, which has hundreds of real runs), so every one
// of these four signals has to agree before redirecting, not just one.
export function useOnboardingGate() {
  const navigate = useNavigate()
  const location = useLocation()
  const { data: stravaStatus } = useStravaStatus()
  const { data: garminStatus } = useGarminStatus()
  const { data: goals } = useGoals()
  const { data: runs } = useAllRuns()

  useEffect(() => {
    if (location.pathname === "/onboarding") return
    if (!stravaStatus || !garminStatus || !goals || !runs) return // wait for every signal to load
    const looksFresh = !stravaStatus.connected && !garminStatus.configured && goals.length === 0 && runs.length === 0
    if (looksFresh) navigate("/onboarding", { replace: true })
  }, [stravaStatus, garminStatus, goals, runs, location.pathname, navigate])
}
