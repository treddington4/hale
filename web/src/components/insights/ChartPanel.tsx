import { useState, type ReactNode } from "react"
import { Maximize2 } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"

// Title/sub/canvas wrapper mirroring the legacy chartCardHTML() layout
// (.chart-card/.chart-title/.chart-sub/.chart-body), plus an empty state for
// when there isn't enough data yet to plot. When there's real data to show,
// an expand button reopens the same `children` inside a wider dialog — each
// mount gets its own canvas ref, so this just creates a second independent
// Chart.js instance rather than trying to move the first one.
export function ChartPanel({
  title,
  sub,
  empty,
  action,
  children,
}: {
  title: string
  sub?: string
  empty?: string | null
  action?: ReactNode
  children?: ReactNode
}) {
  const [expanded, setExpanded] = useState(false)

  return (
    <Card className="gap-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-bold">{title}</div>
          {sub && <div className="text-muted-foreground text-xs">{sub}</div>}
        </div>
        <div className="flex items-center gap-1">
          {action}
          {!empty && children && (
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={() => setExpanded(true)}
              aria-label={`Expand ${title}`}
            >
              <Maximize2 className="size-3.5" />
            </Button>
          )}
        </div>
      </div>
      {empty ? (
        <div className="text-hale-faint flex h-24 items-center justify-center text-xs">{empty}</div>
      ) : (
        children
      )}
      {!empty && children && (
        <Dialog open={expanded} onOpenChange={setExpanded}>
          <DialogContent className="max-h-[90vh] w-full max-w-4xl overflow-y-auto">
            <DialogHeader>
              <DialogTitle>{title}</DialogTitle>
              {sub && <div className="text-muted-foreground text-xs">{sub}</div>}
            </DialogHeader>
            {children}
          </DialogContent>
        </Dialog>
      )}
    </Card>
  )
}
