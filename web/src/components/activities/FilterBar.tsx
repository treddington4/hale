import { ChevronLeft, ChevronRight } from "lucide-react"
import type { FilterMode } from "@/lib/dates"
import { currentFilterRange, fmtRangeLabel, toDateInputValue, todayMidnight } from "@/lib/dates"
import { cn } from "@/lib/utils"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"

const MODES: { mode: FilterMode; label: string }[] = [
  { mode: "rolling7", label: "7 Days" },
  { mode: "week", label: "Week" },
  { mode: "month", label: "Month" },
  { mode: "sixMonths", label: "6 Months" },
  { mode: "year", label: "Year" },
  { mode: "ytd", label: "YTD" },
  { mode: "custom", label: "Custom" },
  { mode: "all", label: "All" },
]

export interface FilterState {
  mode: FilterMode
  anchor: Date
  customStart: Date
  customEnd: Date
  activityType: string
}

export function FilterBar({
  state,
  onChange,
  activityTypeCounts,
}: {
  state: FilterState
  onChange: (next: FilterState) => void
  activityTypeCounts: { type: string; count: number }[]
}) {
  const NAVIGABLE_MODES: FilterMode[] = ["rolling7", "week", "month", "sixMonths", "year"]
  const navigable = NAVIGABLE_MODES.includes(state.mode)
  const { start, end } = currentFilterRange(state.mode, state.anchor, state.customStart, state.customEnd)
  const today = todayMidnight()
  // Page by the window's own length (7d/30d/182d/365d) rather than a fixed 7
  // days — a 7-day step would take forever to click through a Year view, and
  // this keeps the buttons consistent with drag-to-scroll's shift-by-one-
  // window-per-swipe behavior on the charts below (InsightsPage's
  // handleWindowDrag).
  const windowDays = Math.round((end.getTime() - start.getTime()) / 86400000) + 1

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1">
        {MODES.map(({ mode, label }) => (
          <button
            key={mode}
            className={cn(
              "rounded-md px-3 py-1 text-xs font-medium transition-colors",
              state.mode === mode
                ? "bg-secondary text-foreground"
                : "text-muted-foreground hover:bg-secondary/60",
            )}
            onClick={() => onChange({ ...state, mode, anchor: todayMidnight() })}
          >
            {label}
          </button>
        ))}
      </div>

      {navigable && (
        <div className="flex items-center gap-2">
          <button
            className="text-muted-foreground hover:text-foreground"
            onClick={() => onChange({ ...state, anchor: new Date(state.anchor.getTime() - windowDays * 86400000) })}
          >
            <ChevronLeft className="size-4" />
          </button>
          <span className="text-muted-foreground w-36 text-center font-mono text-xs">{fmtRangeLabel(start, end)}</span>
          <button
            className="text-muted-foreground hover:text-foreground disabled:opacity-30"
            disabled={end >= today}
            onClick={() => {
              const next = new Date(state.anchor.getTime() + windowDays * 86400000)
              onChange({ ...state, anchor: next > today ? today : next })
            }}
          >
            <ChevronRight className="size-4" />
          </button>
        </div>
      )}

      {state.mode === "custom" && (
        <div className="flex items-center gap-2">
          <input
            type="date"
            className="border-input bg-background rounded-md border px-2 py-1 text-xs"
            value={toDateInputValue(state.customStart)}
            onChange={(e) => e.target.value && onChange({ ...state, customStart: new Date(e.target.value + "T00:00:00") })}
          />
          <span className="text-muted-foreground text-xs">to</span>
          <input
            type="date"
            className="border-input bg-background rounded-md border px-2 py-1 text-xs"
            value={toDateInputValue(state.customEnd)}
            onChange={(e) => e.target.value && onChange({ ...state, customEnd: new Date(e.target.value + "T00:00:00") })}
          />
        </div>
      )}

      <Select value={state.activityType} onValueChange={(v) => onChange({ ...state, activityType: v })}>
        <SelectTrigger className="w-48">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All types · {activityTypeCounts.reduce((s, t) => s + t.count, 0)}</SelectItem>
          {activityTypeCounts.map(({ type, count }) => (
            <SelectItem key={type} value={type}>
              {type} · {count}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
