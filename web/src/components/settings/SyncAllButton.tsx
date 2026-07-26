import { useState } from "react"
import { useStravaStatus, useGarminStatus, useHevyStatus, useSettingsMutations } from "@/hooks/useSettings"
import { Button } from "@/components/ui/button"

export function SyncAllButton() {
  const { data: stravaStatus } = useStravaStatus()
  const { data: garminStatus } = useGarminStatus()
  const { data: hevyStatus } = useHevyStatus()
  const { manualSync } = useSettingsMutations()
  const [syncing, setSyncing] = useState(false)

  const connectedSources = [
    stravaStatus?.connected ? "strava" : null,
    garminStatus?.configured ? "garmin" : null,
    hevyStatus?.configured ? "hevy" : null,
  ].filter(Boolean)

  function handleSyncAll() {
    if (connectedSources.length === 0) return
    setSyncing(true)
    Promise.all(connectedSources.map((source) => manualSync.mutateAsync(source as any))).finally(() =>
      setSyncing(false),
    )
  }

  if (connectedSources.length === 0) return null

  return (
    <Button onClick={handleSyncAll} disabled={syncing || manualSync.isPending}>
      {syncing ? "Syncing…" : "Sync All"}
    </Button>
  )
}
