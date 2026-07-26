import { useState } from "react"
import {
  useStravaStatus,
  useGarminStatus,
  useHevyStatus,
  useSyncMeta,
  useConnections,
  useSettingsMutations,
  useConfig,
} from "@/hooks/useSettings"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ConnectionTile } from "./ConnectionTile"

function SettingsRow({ label, value }: { label?: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 text-[13px] py-1">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono text-xs">{value}</span>
    </div>
  )
}

export function ConnectionsGrid() {
  const { data: config } = useConfig()
  const { data: stravaStatus } = useStravaStatus()
  const { data: garminStatus } = useGarminStatus()
  const { data: hevyStatus } = useHevyStatus()
  const { data: syncMeta } = useSyncMeta()
  const { data: connections } = useConnections()
  const { saveGarminConnection, saveHevyConnection, deleteConnection, garminImport } = useSettingsMutations()

  const [expandedTile, setExpandedTile] = useState<string | null>(null)
  const [garminUsername, setGarminUsername] = useState("")
  const [garminPassword, setGarminPassword] = useState("")
  const [hevyApiKey, setHevyApiKey] = useState("")
  const [garminFile, setGarminFile] = useState<File | null>(null)
  const [noFileError, setNoFileError] = useState(false)

  const garminConn = connections?.find((c) => c.provider === "garmin")
  const hevyConn = connections?.find((c) => c.provider === "hevy")
  const hevySaveFailed = saveHevyConnection.data && !saveHevyConnection.data.ok ? saveHevyConnection.data.message : null

  function handleGarminSave() {
    if (!garminUsername || !garminPassword) return
    saveGarminConnection.mutate({ username: garminUsername.trim(), password: garminPassword })
  }

  function handleHevySave() {
    if (!hevyApiKey) return
    saveHevyConnection.mutate(hevyApiKey.trim(), { onSuccess: () => setHevyApiKey("") })
  }

  function handleGarminImport() {
    if (!garminFile) {
      setNoFileError(true)
      return
    }
    setNoFileError(false)
    garminImport.mutate(garminFile)
  }

  if (config?.isDemoUser) {
    return (
      <Card className="gap-2">
        <div className="mb-1 text-sm font-bold">Connections</div>
        <div className="text-hale-faint text-xs">Not available in the demo.</div>
      </Card>
    )
  }

  return (
    <Card className="gap-3">
      <div className="mb-3 text-sm font-bold">Connections</div>

      <div className="grid grid-cols-3 gap-2">
        {/* Strava */}
        <button
          onClick={() => setExpandedTile(expandedTile === "strava" ? null : "strava")}
          className="text-left"
        >
          <ConnectionTile name="Strava" icon="🔴" connected={!!stravaStatus?.connected} onClick={() => {}} />
        </button>

        {/* Garmin */}
        <button onClick={() => setExpandedTile(expandedTile === "garmin" ? null : "garmin")} className="text-left">
          <ConnectionTile name="Garmin" icon="🏃" connected={!!garminStatus?.configured} onClick={() => {}} />
        </button>

        {/* Hevy */}
        <button onClick={() => setExpandedTile(expandedTile === "hevy" ? null : "hevy")} className="text-left">
          <ConnectionTile name="Hevy" icon="💪" connected={!!hevyStatus?.configured} onClick={() => {}} />
        </button>
      </div>

      {/* Expanded sections */}
      {expandedTile === "strava" && (
        <div className="border-t pt-3 mt-3 space-y-2">
          <div className="text-sm font-medium">Strava</div>
          {syncMeta?.strava && (
            <>
              <SettingsRow
                label="Last synced"
                value={
                  syncMeta.strava.lastSyncedAt ? new Date(syncMeta.strava.lastSyncedAt).toLocaleString() : "Never"
                }
              />
              {syncMeta.strava.lastError && (
                <div className="text-hale-hot text-xs">{syncMeta.strava.lastError}</div>
              )}
            </>
          )}
          {!stravaStatus?.connected && (
            <Button size="sm" asChild className="w-full">
              <a href="/auth/strava/login">Connect Strava</a>
            </Button>
          )}
        </div>
      )}

      {expandedTile === "garmin" && (
        <div className="border-t pt-3 mt-3 space-y-3">
          <div className="text-sm font-medium">Garmin</div>
          <p className="text-xs text-muted-foreground">
            Manages your Garmin login. Safe to re-enter if you change your password.
          </p>

          {garminStatus?.configured ? (
            <>
              <SettingsRow label="Username" value={garminConn?.username ?? ""} />
              {syncMeta?.garmin && (
                <>
                  <SettingsRow
                    label="Last synced"
                    value={
                      syncMeta.garmin.lastSyncedAt
                        ? new Date(syncMeta.garmin.lastSyncedAt).toLocaleString()
                        : "Never"
                    }
                  />
                  {syncMeta.garmin.lastError && (
                    <div className="text-hale-hot text-xs">{syncMeta.garmin.lastError}</div>
                  )}
                </>
              )}
              <Button
                variant="outline"
                size="sm"
                className="text-hale-hot"
                onClick={() => deleteConnection.mutate("garmin")}
              >
                Disconnect
              </Button>
            </>
          ) : (
            <>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs">Garmin email</Label>
                <Input
                  className="text-xs"
                  placeholder="you@example.com"
                  value={garminUsername}
                  onChange={(e) => setGarminUsername(e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs">Garmin password</Label>
                <Input
                  className="text-xs"
                  type="password"
                  value={garminPassword}
                  onChange={(e) => setGarminPassword(e.target.value)}
                />
              </div>
              <Button
                size="sm"
                disabled={!garminUsername || !garminPassword || saveGarminConnection.isPending}
                onClick={handleGarminSave}
                className="w-full"
              >
                {saveGarminConnection.isPending ? "Saving…" : "Save connection"}
              </Button>
            </>
          )}

          {/* Garmin import section */}
          <div className="border-t pt-3">
            <p className="text-xs text-muted-foreground mb-2">
              Upload the ZIP from Garmin's "Export Your Data" to backfill history.
            </p>
            <div className="flex items-center gap-2">
              <input
                type="file"
                accept=".zip"
                onChange={(e) => setGarminFile(e.target.files?.[0] ?? null)}
                className="text-xs flex-1"
              />
              <Button size="sm" disabled={garminImport.isPending} onClick={handleGarminImport}>
                {garminImport.isPending ? "…" : "Import"}
              </Button>
            </div>
            {noFileError && <div className="text-hale-hot text-xs mt-1">Choose a .zip file first</div>}
            {garminImport.data &&
              (garminImport.data.ok ? (
                <div className="text-muted-foreground text-xs mt-2">
                  Imported {garminImport.data.summary.activitiesImported} activities
                </div>
              ) : (
                <div className="text-hale-hot text-xs mt-2">{garminImport.data.message}</div>
              ))}
          </div>
        </div>
      )}

      {expandedTile === "hevy" && (
        <div className="border-t pt-3 mt-3 space-y-3">
          <div className="text-sm font-medium">Hevy</div>
          <p className="text-xs text-muted-foreground">
            Auto-syncs your logged strength workouts. Get your API key from Hevy app Settings → API.
          </p>

          {hevyStatus?.configured ? (
            <>
              {syncMeta?.hevy && (
                <>
                  <SettingsRow
                    label="Last synced"
                    value={
                      syncMeta.hevy.lastSyncedAt
                        ? new Date(syncMeta.hevy.lastSyncedAt).toLocaleString()
                        : "Never"
                    }
                  />
                  {syncMeta.hevy.lastError && (
                    <div className="text-hale-hot text-xs">{syncMeta.hevy.lastError}</div>
                  )}
                </>
              )}
              <Button
                variant="outline"
                size="sm"
                className="text-hale-hot"
                onClick={() => deleteConnection.mutate("hevy")}
              >
                Disconnect
              </Button>
            </>
          ) : (
            <>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs">API key</Label>
                <Input
                  className="text-xs"
                  type="password"
                  placeholder={hevyConn ? "••••••••" : "Paste your Hevy API key"}
                  value={hevyApiKey}
                  onChange={(e) => setHevyApiKey(e.target.value)}
                />
              </div>
              <Button
                size="sm"
                disabled={!hevyApiKey || saveHevyConnection.isPending}
                onClick={handleHevySave}
                className="w-full"
              >
                {saveHevyConnection.isPending ? "Validating…" : "Save connection"}
              </Button>
              {hevySaveFailed && <div className="text-hale-hot text-xs">{hevySaveFailed}</div>}
            </>
          )}
        </div>
      )}
    </Card>
  )
}
