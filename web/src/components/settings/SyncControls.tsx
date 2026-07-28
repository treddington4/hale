import type { SyncJob, BacklogJob, SyncSource } from "@/lib/api"
import { useSyncStatus, useBacklogStatus, useSettingsMutations } from "@/hooks/useSettings"
import { Button } from "@/components/ui/button"

function LogBlock({ lines }: { lines: string[] }) {
  return (
    <pre className="bg-background border-border text-muted-foreground mt-1.5 max-h-40 overflow-y-auto rounded-md border p-2 font-mono text-[11px] whitespace-pre-wrap">
      {lines.join("\n")}
    </pre>
  )
}

// Mirrors renderSyncPanel()/renderBacklogPanel() — either a "failed to start"
// message from the button click itself, or the polled job's own status text.
function JobPanel({
  job,
  startError,
  kind,
}: {
  job?: SyncJob | BacklogJob
  startError?: string | null
  kind: "sync" | "backlog"
}) {
  if (startError) return <div className="text-hale-hot mt-2 text-xs">{startError}</div>
  if (!job) return null

  if (job.status === "running") {
    return (
      <div className="mt-2">
        <div className="text-muted-foreground text-xs">
          {job.count} {job.count === 1 ? "activity" : "activities"} synced so far…
        </div>
        {job.log.length > 0 && <LogBlock lines={job.log} />}
      </div>
    )
  }
  if (job.status === "error") {
    return (
      <div className="mt-2">
        <div className="text-hale-hot text-xs">Failed: {job.error || "unknown error"}</div>
        {job.log.length > 0 && <LogBlock lines={job.log} />}
      </div>
    )
  }
  if (kind === "sync" && job.status === "done") {
    return (
      <div className="mt-2">
        <div className="text-muted-foreground text-xs">
          Synced {job.count} {job.count === 1 ? "activity" : "activities"}
          {job.finishedAt && ` · ${new Date(job.finishedAt).toLocaleString()}`}
        </div>
        {job.log.length > 0 && <LogBlock lines={job.log} />}
      </div>
    )
  }
  if (kind === "backlog" && "lastCompleted" in job && job.lastCompleted.syncedAt) {
    return (
      <div className="mt-2">
        <div className="text-muted-foreground text-xs">
          Last backlog sync: {new Date(job.lastCompleted.syncedAt).toLocaleString()} · {job.lastCompleted.count} activities
        </div>
        {job.log.length > 0 && <LogBlock lines={job.log} />}
      </div>
    )
  }
  return null
}

export function SyncControls({ source, enabled }: { source: SyncSource; enabled: boolean }) {
  const syncStatusQuery = useSyncStatus(source)
  const backlogStatusQuery = useBacklogStatus(source)
  const { manualSync, backlogSync } = useSettingsMutations()

  const syncJob = syncStatusQuery.data
  const backlogJob = backlogStatusQuery.data
  const syncRunning = syncJob?.status === "running"
  const backlogRunning = backlogJob?.status === "running"

  const syncStartFailed = manualSync.data && !manualSync.data.ok ? manualSync.data.message : null
  const backlogStartFailed = backlogSync.data && !backlogSync.data.ok ? backlogSync.data.message : null

  return (
    <>
      <div className="mt-2.5 flex gap-2">
        <Button size="sm" disabled={!enabled || syncRunning} onClick={() => manualSync.mutate(source)}>
          {syncRunning ? "Syncing…" : "Sync Now"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!enabled || backlogRunning}
          onClick={() => backlogSync.mutate(source)}
        >
          {backlogRunning
            ? "Backlog Sync Running…"
            : backlogJob?.lastCompleted.syncedAt
              ? "Re-run Backlog Sync"
              : "Run Backlog Sync"}
        </Button>
      </div>
      <JobPanel job={syncJob} startError={syncStartFailed} kind="sync" />
      <JobPanel job={backlogJob} startError={backlogStartFailed} kind="backlog" />
    </>
  )
}
