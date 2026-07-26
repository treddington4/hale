import { useDailyCoachReport } from "@/hooks/useDailyCoachReport"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

// First load of a new calendar day can take a few seconds (the report is generated
// lazily on-demand, see routes/chat.py's GET /api/coach/daily-report) — show a
// skeleton rather than nothing while that's in flight, unlike the other Home cards'
// "return null while loading" convention, since this one is meant to read first.
// Degrades to nothing (not an empty-state card) once loaded if chat isn't configured
// or generation failed/declined today — matches GearWearCard/WellnessCards' pattern.
export function DailyCoachReportCard() {
  const { data } = useDailyCoachReport()

  if (data === undefined) return <Skeleton className="h-24 w-full" />
  if (!data.report) return null

  return (
    <div className="mt-6">
      <h2 className="mb-3 text-sm font-semibold">Coach's Daily Check-in</h2>
      <Card className="text-sm leading-relaxed whitespace-pre-line">{data.report}</Card>
    </div>
  )
}
