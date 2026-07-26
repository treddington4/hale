import { useState, useEffect, type ReactNode } from "react"
import {
  useConfig,
  useTokens,
  useProfile,
  useUpdateProfile,
  useSettingsMutations,
} from "@/hooks/useSettings"
import { useCoachPersonality, useCoachIssue, useRefreshCoachIssue, useClearCoachIssue } from "@/hooks/useChat"
import { usePush } from "@/hooks/usePush"
import { useTrainingConfig, useUpdateTrainingConfig } from "@/hooks/useWorkouts"
import type { CoachPersonality, ApiTokenCreated, Sex } from "@/lib/api"
import { GearSection } from "@/components/settings/GearSection"
import { ConnectionsGrid } from "@/components/settings/ConnectionsGrid"
import { SyncAllButton } from "@/components/settings/SyncAllButton"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { renderMarkdownLite } from "@/lib/markdownLite"
import { copyToClipboard } from "@/lib/clipboard"

function SettingsSection({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <Card className="gap-2">
      <div className="mb-1 text-sm font-bold">{title}</div>
      {children}
    </Card>
  )
}

function SettingsRow({ label, value }: { label?: ReactNode; value: ReactNode }) {
  return (
    <div className="border-border flex items-center justify-between gap-3 border-t py-2 text-[13px] first:border-t-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  )
}


function CoachSection() {
  const { data } = useCoachPersonality()
  const { setCoachPersonality } = useSettingsMutations()
  const [saved, setSaved] = useState(false)

  function handleChange(v: string) {
    setCoachPersonality.mutate(v as CoachPersonality, {
      onSuccess: () => {
        setSaved(true)
        setTimeout(() => setSaved(false), 1500)
      },
    })
  }

  return (
    <SettingsSection title="Coach">
      <div className="flex items-center justify-between py-2 text-[13px]">
        <span className="text-muted-foreground">Personality</span>
        <Select value={data?.personality ?? "normal"} onValueChange={handleChange}>
          <SelectTrigger className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="encouraging">Encouraging</SelectItem>
            <SelectItem value="normal">Normal</SelectItem>
            <SelectItem value="spicy">Spicy</SelectItem>
            <SelectItem value="insulting">Insulting</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {saved && <div className="text-hale-good text-xs">Saved</div>}
    </SettingsSection>
  )
}

// Phase 12.5 — surfaces the rolling draft GitHub issue (see coach/self_review.py):
// findings from the periodic chat-history review plus anything the coach logged live
// via log_product_feedback (bug reports/feature requests it correctly routed instead
// of trying to answer as a coaching question). Draft-only — downloading/clearing here
// never posts anything to github.com itself; publishing is a manual step.
function CoachFeedbackSection() {
  const { data: draft, isPending } = useCoachIssue()
  const refreshIssue = useRefreshCoachIssue()
  const clearIssue = useClearCoachIssue()
  const [previewOpen, setPreviewOpen] = useState(false)
  const [copied, setCopied] = useState(false)

  if (isPending || !draft) return null

  const fullText = `# ${draft.title}\n\n${draft.body}`

  function handleDownload() {
    const blob = new Blob([fullText], { type: "text/markdown" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `hale-coach-feedback-${new Date().toISOString().slice(0, 10)}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  function handleCopy() {
    copyToClipboard(fullText).then((ok) => {
      if (!ok) return
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  function handlePreview() {
    // Opens immediately with what's already cached, then re-checks for anything said
    // since the last review — cheap when nothing's new (the backend's own checkpoint
    // short-circuits before any LLM call), so this doesn't feel wasteful on repeat
    // clicks. The dialog's content swaps in place if the refresh finds something.
    setPreviewOpen(true)
    refreshIssue.mutate()
  }

  return (
    <SettingsSection title="Coach Feedback">
      <div className="text-hale-faint pb-2 text-xs">
        {draft.frustrationCount} item{draft.frustrationCount === 1 ? "" : "s"} logged — last updated{" "}
        {new Date(draft.updatedAt).toLocaleString()}. Draft only; nothing here is posted to GitHub automatically.
      </div>
      <div className="flex flex-wrap gap-2">
        {/* Downloading a .md just triggers a save on most mobile browsers with no easy
            way to actually read it — this reads the same content in place instead. */}
        <Button size="sm" variant="outline" onClick={handlePreview}>
          Preview
        </Button>
        <Button size="sm" onClick={handleDownload}>
          Download as .md
        </Button>
        <Button size="sm" variant="outline" disabled={clearIssue.isPending} onClick={() => clearIssue.mutate()}>
          {clearIssue.isPending ? "Clearing…" : "Clear"}
        </Button>
      </div>

      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{draft.title}</DialogTitle>
          </DialogHeader>
          {refreshIssue.isPending && (
            <div className="text-hale-faint text-xs">Checking for anything new…</div>
          )}
          <div className="max-h-[60vh] overflow-y-auto text-xs">
            <div className="text-muted-foreground">{renderMarkdownLite(draft.body)}</div>
            <Button size="sm" variant="outline" className="mt-3" onClick={handleCopy}>
              {copied ? "Copied!" : "Copy all"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </SettingsSection>
  )
}

// Height is entered as feet+inches (matching this app's imperial-first
// convention elsewhere) but stored/sent as total inches — same "one internal
// unit, activity/context-aware entry layer" pattern as WorkoutFormDialog's
// Ride mph<->sec/mi conversion.
function ProfileSection() {
  const { data: profile } = useProfile()
  const updateProfile = useUpdateProfile()
  const [heightFt, setHeightFt] = useState("")
  const [heightIn, setHeightIn] = useState("")
  const [weightLb, setWeightLb] = useState("")
  const [dateOfBirth, setDateOfBirth] = useState("")
  const [sex, setSex] = useState<Sex | "">("")
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!profile) return
    if (profile.heightIn) {
      setHeightFt(String(Math.floor(profile.heightIn / 12)))
      setHeightIn(String(Math.round(profile.heightIn % 12)))
    } else {
      setHeightFt("")
      setHeightIn("")
    }
    setWeightLb(profile.weightLb?.toString() ?? "")
    setDateOfBirth(profile.dateOfBirth ?? "")
    setSex(profile.sex ?? "")
  }, [profile])

  function handleSave() {
    const ft = Number(heightFt) || 0
    const inches = Number(heightIn) || 0
    const totalIn = ft || inches ? ft * 12 + inches : null
    updateProfile.mutate(
      {
        heightIn: totalIn,
        weightLb: weightLb ? Number(weightLb) : null,
        dateOfBirth: dateOfBirth || null,
        sex: sex || null,
      },
      { onSuccess: () => { setSaved(true); setTimeout(() => setSaved(false), 1500) } },
    )
  }

  return (
    <SettingsSection title="Profile">
      <div className="text-hale-faint pb-2 text-xs">
        Body-metric fields for future features (e.g. an energy-availability
        estimate) — nothing reads these yet.
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label>Height</Label>
          <div className="flex gap-2">
            <div className="flex flex-1 items-center gap-1">
              <Input type="number" placeholder="ft" value={heightFt} onChange={(e) => setHeightFt(e.target.value)} />
              <span className="text-muted-foreground text-xs">ft</span>
            </div>
            <div className="flex flex-1 items-center gap-1">
              <Input type="number" placeholder="in" value={heightIn} onChange={(e) => setHeightIn(e.target.value)} />
              <span className="text-muted-foreground text-xs">in</span>
            </div>
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Weight (lb)</Label>
          <Input type="number" value={weightLb} onChange={(e) => setWeightLb(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Date of birth</Label>
          <Input type="date" value={dateOfBirth} onChange={(e) => setDateOfBirth(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Sex</Label>
          <Select value={sex} onValueChange={(v) => setSex(v as Sex)}>
            <SelectTrigger><SelectValue placeholder="—" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="male">Male</SelectItem>
              <SelectItem value="female">Female</SelectItem>
              <SelectItem value="other">Other</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <Button size="sm" className="mt-3" disabled={updateProfile.isPending} onClick={handleSave}>
        {updateProfile.isPending ? "Saving…" : "Save"}
      </Button>
      {saved && <div className="text-hale-good mt-1.5 text-xs">Saved</div>}
    </SettingsSection>
  )
}

function TrainingSection() {
  const { data: config } = useTrainingConfig()
  const updateConfig = useUpdateTrainingConfig()
  const [maxHr, setMaxHr] = useState("")
  const [thresholdHr, setThresholdHr] = useState("")
  const [weeklyRampPct, setWeeklyRampPct] = useState("")
  const [strengthDaysPerWeek, setStrengthDaysPerWeek] = useState("")
  const [mesocyclePattern, setMesocyclePattern] = useState("3:1")
  const [distribution, setDistribution] = useState("pyramidal")
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!config) return
    setMaxHr(config.maxHr?.toString() ?? "")
    setThresholdHr(config.thresholdHr?.toString() ?? "")
    setWeeklyRampPct(config.weeklyRampPct.toString())
    setStrengthDaysPerWeek(config.strengthDaysPerWeek.toString())
    setMesocyclePattern(config.mesocyclePattern)
    setDistribution(config.distribution)
  }, [config])

  function handleSave() {
    updateConfig.mutate(
      {
        maxHr: maxHr ? Number(maxHr) : null,
        thresholdHr: thresholdHr ? Number(thresholdHr) : null,
        weeklyRampPct: Number(weeklyRampPct),
        strengthDaysPerWeek: Number(strengthDaysPerWeek),
        mesocyclePattern,
        distribution,
      },
      { onSuccess: () => { setSaved(true); setTimeout(() => setSaved(false), 1500) } },
    )
  }

  return (
    <SettingsSection title="Training">
      <div className="text-hale-faint pb-2 text-xs">
        Drives the workout generator's zones, ramp rate, and periodization (Phase 4).
        Leave max/threshold HR blank to use age-based defaults.
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label>Max HR</Label>
          <Input type="number" value={maxHr} onChange={(e) => setMaxHr(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Threshold HR</Label>
          <Input type="number" value={thresholdHr} onChange={(e) => setThresholdHr(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Weekly ramp %</Label>
          <Input type="number" value={weeklyRampPct} onChange={(e) => setWeeklyRampPct(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Strength days/week</Label>
          <Input
            type="number"
            value={strengthDaysPerWeek}
            onChange={(e) => setStrengthDaysPerWeek(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Mesocycle pattern</Label>
          <Select value={mesocyclePattern} onValueChange={setMesocyclePattern}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="3:1">3:1</SelectItem>
              <SelectItem value="2:1">2:1</SelectItem>
              <SelectItem value="4:1">4:1</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Distribution</Label>
          <Select value={distribution} onValueChange={setDistribution}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="pyramidal">Pyramidal</SelectItem>
              <SelectItem value="polarized">Polarized</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <Button size="sm" className="mt-3" disabled={updateConfig.isPending} onClick={handleSave}>
        {updateConfig.isPending ? "Saving…" : "Save"}
      </Button>
      {saved && <div className="text-hale-good mt-1.5 text-xs">Saved</div>}
    </SettingsSection>
  )
}

function PushSection() {
  const { data: config } = useConfig()
  const push = usePush()

  // Hidden entirely when the server has no VAPID keypair set — same "clean no-op,
  // not a broken control" degrade as the Chat tab when neither Claude credential is set.
  if (!config?.pushConfigured) return null

  return (
    <SettingsSection title="Push notifications">
      {!push.supported ? (
        <div className="text-hale-faint text-xs">Not supported in this browser.</div>
      ) : (
        <>
          <div className="flex items-center justify-between py-2 text-[13px]">
            <span className="text-muted-foreground">Enable on this device</span>
            <Button
              size="sm"
              variant={push.subscribed ? "outline" : "default"}
              disabled={push.checking || push.enable.isPending || push.disable.isPending}
              onClick={push.toggle}
            >
              {push.subscribed ? "Disable" : "Enable"}
            </Button>
          </div>
          {push.subscribed && (
            <Button
              size="sm"
              variant="link"
              className="h-auto p-0"
              disabled={push.sendTest.isPending}
              onClick={() => push.sendTest.mutate()}
            >
              {push.sendTest.isPending ? "Sending…" : "Send test notification"}
            </Button>
          )}
          {push.sendTest.isSuccess && (
            <div className="text-hale-good mt-1 text-xs">
              Sent to {push.sendTest.data?.sent ?? 0} device{push.sendTest.data?.sent === 1 ? "" : "s"}.
            </div>
          )}
          {(push.enable.isError || push.disable.isError) && (
            <div className="text-hale-hot mt-1 text-xs">
              {(push.enable.error || push.disable.error)?.message || "Something went wrong"}
            </div>
          )}
        </>
      )}
    </SettingsSection>
  )
}

function SyncScheduleSection() {
  const { data: config } = useConfig()
  return (
    <SettingsSection title="Sync schedule">
      <SettingsRow label="Auto-sync interval" value={`Every ${config?.syncIntervalHours ?? "--"}h`} />
      <SettingsRow label="Activities per sync" value={config?.syncActivityLimit ?? "--"} />
      <div className="text-hale-faint pt-2 text-xs">
        Auto-sync pulls recent activities from Strava every interval and polls Garmin separately. Backlog Sync pulls
        a source's entire history in the background — a one-time catch-up, not part of the regular schedule.
      </div>
    </SettingsSection>
  )
}

function AboutSection() {
  return (
    <SettingsSection title="About">
      <div className="text-hale-faint text-xs">
        HALE — HALE's Adaptive Life Engine — is free and open source. Found a bug, want a feature, or just want to
        support the project?
      </div>
      <div className="mt-2.5">
        <Button variant="outline" size="sm" asChild>
          <a href="https://github.com/treddington4/hale" target="_blank" rel="noopener noreferrer">
            Contribute / Donate on GitHub
          </a>
        </Button>
      </div>
    </SettingsSection>
  )
}

function TokensSection() {
  const { data: tokens } = useTokens()
  const { createToken, deleteToken } = useSettingsMutations()
  const [name, setName] = useState("")
  const [justCreated, setJustCreated] = useState<ApiTokenCreated | null>(null)

  function handleCreate() {
    createToken.mutate(name.trim() || "Unnamed device", {
      onSuccess: (created: ApiTokenCreated) => {
        setJustCreated(created)
        setName("")
      },
    })
  }

  return (
    <SettingsSection title="API Tokens">
      <div className="text-hale-faint pb-2 text-xs">
        Device tokens for headless/API clients (e.g. a future mobile client, or a script
        calling the ingest API). Only meaningful once real auth is turned on via the
        AUTH_MODE env var — see app/auth.py.
      </div>
      {justCreated && (
        <div className="border-hale-good bg-background mb-3 rounded-md border p-3">
          <div className="text-hale-good text-xs font-semibold">Copy this token now — it won't be shown again</div>
          <code className="mt-1 block font-mono text-xs break-all">{justCreated.token}</code>
          <Button variant="link" size="sm" className="mt-1 h-auto p-0" onClick={() => setJustCreated(null)}>
            Dismiss
          </Button>
        </div>
      )}
      <div className="flex gap-2">
        <Input
          placeholder="Token name (e.g. Pixel 9 Pro)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button size="sm" disabled={createToken.isPending} onClick={handleCreate}>
          {createToken.isPending ? "Creating…" : "Create token"}
        </Button>
      </div>
      {tokens && tokens.length > 0 && (
        <div className="mt-3 flex flex-col gap-2">
          {tokens.map((t) => (
            <div key={t.id} className="border-border flex items-center justify-between border-t pt-2 text-[13px]">
              <div>
                <div>{t.name || "Unnamed"}</div>
                <div className="text-hale-faint text-xs">
                  Created {new Date(t.createdAt).toLocaleDateString()}
                  {t.lastUsedAt ? ` · last used ${new Date(t.lastUsedAt).toLocaleDateString()}` : " · never used"}
                </div>
              </div>
              <Button
                variant="link"
                size="sm"
                className="text-hale-hot h-auto p-0"
                onClick={() => deleteToken.mutate(t.id)}
              >
                Revoke
              </Button>
            </div>
          ))}
        </div>
      )}
    </SettingsSection>
  )
}

export function SettingsPage() {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3 mb-2">
        <h2 className="text-lg font-semibold">Settings</h2>
        <SyncAllButton />
      </div>
      <ConnectionsGrid />
      <TokensSection />
      <CoachSection />
      <CoachFeedbackSection />
      <ProfileSection />
      <TrainingSection />
      <GearSection />
      <PushSection />
      <SyncScheduleSection />
      <AboutSection />
    </div>
  )
}
