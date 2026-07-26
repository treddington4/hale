import { useState } from "react"
import { Footprints, Bike, Dumbbell, HeartPulse, ChevronLeft } from "lucide-react"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { useQuickGenerate, useRecoveryTools } from "@/hooks/useWorkouts"
import type { QuickGenerateDomain, QuickGenerateResult, Workout, RecoverySession } from "@/lib/api"
import { WorkoutCard } from "@/components/workouts/WorkoutCard"
import { RecoverySessionCard } from "@/components/workouts/RecoverySessionCard"

// Matches STRENGTH_TEMPLATES' keys in app/coach/generator.py exactly. `null` means
// "no override" (let the backend auto-pick from recent cardio volume).
const STRENGTH_TEMPLATES: { key: string | null; label: string }[] = [
  { key: null, label: "Auto" },
  { key: "full_body_ab", label: "Full Body" },
  { key: "runner_focus", label: "Runner Focus" },
  { key: "back_and_legs", label: "Back & Legs" },
  { key: "womens_at_home", label: "Women's — At Home" },
  { key: "womens_at_gym", label: "Women's — At Gym" },
]

const TYPE_BUTTONS: { domain: QuickGenerateDomain; label: string; icon: typeof Footprints }[] = [
  { domain: "run", label: "Run", icon: Footprints },
  { domain: "ride", label: "Ride", icon: Bike },
  { domain: "strength", label: "Strength", icon: Dumbbell },
  { domain: "recovery", label: "Recovery", icon: HeartPulse },
]

type Step = "pick" | "preview"

// Phase 14.6 — replaces the old always-visible QuickGenerateBar (which fired
// immediately on click, no chance to back out) and the separate "+ New Workout"
// button. One flow: pick a type (or Custom) -> preview the real computed
// prescription -> Confirm actually saves it. Nothing is written until Confirm.
export function NewWorkoutDialog({
  open,
  onOpenChange,
  onCustom,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCustom: () => void
}) {
  const quickGenerate = useQuickGenerate()
  const { data: recoveryTools } = useRecoveryTools()
  const [step, setStep] = useState<Step>("pick")
  const [domain, setDomain] = useState<QuickGenerateDomain | null>(null)
  const [templateOverride, setTemplateOverride] = useState<string | null>(null)
  const [preview, setPreview] = useState<QuickGenerateResult | null>(null)
  const [previewError, setPreviewError] = useState(false)

  function reset() {
    setStep("pick")
    setDomain(null)
    setTemplateOverride(null)
    setPreview(null)
    setPreviewError(false)
  }

  function handleOpenChange(o: boolean) {
    onOpenChange(o)
    if (!o) reset()
  }

  function fetchPreview(d: QuickGenerateDomain, tmpl?: string) {
    setPreviewError(false)
    quickGenerate.mutate(
      { domain: d, templateOverride: tmpl, dryRun: true },
      {
        onSuccess: (result) => {
          setPreview(result)
          setStep("preview")
        },
        onError: () => setPreviewError(true),
      },
    )
  }

  function pick(d: QuickGenerateDomain) {
    setDomain(d)
    setPreviewError(false)
    if (d === "strength") return // wait for a template chip pick below
    fetchPreview(d)
  }

  function confirm() {
    if (!domain) return
    quickGenerate.mutate(
      { domain, templateOverride: templateOverride ?? undefined, dryRun: false },
      { onSuccess: () => handleOpenChange(false) },
    )
  }

  const noRecoveryTool = domain === "recovery" && recoveryTools !== undefined && recoveryTools.length === 0

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New Workout</DialogTitle>
        </DialogHeader>

        {step === "pick" && (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-2">
              {TYPE_BUTTONS.map(({ domain: d, label, icon: Icon }) => (
                <Button
                  key={d}
                  variant="outline"
                  className="h-auto flex-col gap-1.5 py-4"
                  disabled={quickGenerate.isPending}
                  onClick={() => pick(d)}
                >
                  <Icon className="size-5" />
                  <span className="text-sm">{label}</span>
                </Button>
              ))}
            </div>

            {domain === "strength" && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-muted-foreground text-xs">Target:</span>
                {STRENGTH_TEMPLATES.map(({ key, label }) => (
                  <button
                    key={label}
                    type="button"
                    disabled={quickGenerate.isPending}
                    onClick={() => {
                      setTemplateOverride(key)
                      fetchPreview("strength", key ?? undefined)
                    }}
                    className={`rounded-full border px-2.5 py-1 text-xs transition-colors disabled:opacity-50 ${
                      templateOverride === key
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-input bg-transparent hover:bg-accent"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}

            {noRecoveryTool && (
              <p className="text-hale-hot text-xs">No recovery tool set up yet — add one via chat first.</p>
            )}
            {previewError && <p className="text-hale-hot text-xs">Couldn't generate a preview — try again.</p>}
            {quickGenerate.isPending && <p className="text-muted-foreground text-xs">Generating preview…</p>}

            <Button
              variant="outline"
              onClick={() => {
                handleOpenChange(false)
                onCustom()
              }}
            >
              Custom
            </Button>
          </div>
        )}

        {step === "preview" && preview && (
          <div className="flex flex-col gap-3">
            {preview.domain === "recovery" ? (
              <RecoverySessionCard session={preview.result as RecoverySession} preview />
            ) : (
              <WorkoutCard workout={preview.result as Workout} preview />
            )}
            <div className="flex gap-2">
              <Button variant="outline" className="flex-1" onClick={() => setStep("pick")}>
                <ChevronLeft className="size-4" /> Back
              </Button>
              <Button className="flex-1" disabled={quickGenerate.isPending} onClick={confirm}>
                {quickGenerate.isPending ? "Saving…" : "Confirm"}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
