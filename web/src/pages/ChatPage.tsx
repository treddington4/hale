import { useEffect, useMemo, useRef, useState } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import { MessageCircle } from "lucide-react"
import { api, type ChatMessage } from "@/lib/api"
import { useChatStatus, useChatHistory, useCoachPersonality } from "@/hooks/useChat"
import { ChatBubble, type DisplayMessage } from "@/components/chat/ChatBubble"
import { ChatInputBar } from "@/components/chat/ChatInputBar"
import { EmptyState } from "@/components/ui/empty-state"
import { Skeleton } from "@/components/ui/skeleton"

const CHAT_COLLAPSE_VISIBLE_COUNT = 2

// Short UI glosses of coach.py's PERSONA_PROMPTS tones — not the actual system
// prompt text, just enough for the empty state to say who you're talking to.
const PERSONA_LABELS: Record<string, { name: string; blurb: string }> = {
  encouraging: { name: "Encouraging Coach", blurb: "Warm and patient — every session counts as real progress." },
  normal: { name: "Coach", blurb: "Straightforward, data-driven training advice." },
  spicy: { name: "Spicy Coach", blurb: "A little trash talk, always on your side." },
  insulting: { name: "Brutal Coach", blurb: "Blunt and unsparing — bring something to prove." },
}

export function ChatPage() {
  const statusQuery = useChatStatus()
  const historyQuery = useChatHistory()
  const personaQuery = useCoachPersonality()
  const qc = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()

  const [pendingUserText, setPendingUserText] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [historyExpanded, setHistoryExpanded] = useState(false)
  const prefillFired = useRef(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const allMessages = useMemo<DisplayMessage[]>(() => {
    const history = historyQuery.data ?? []
    if (!pendingUserText) return history
    return [
      ...history,
      { role: "user", content: pendingUserText, toolCalls: null, charts: null },
      { role: "assistant", content: "Thinking…", pending: true },
    ]
  }, [historyQuery.data, pendingUserText])

  const shouldCollapse = !historyExpanded && allMessages.length > CHAT_COLLAPSE_VISIBLE_COUNT
  const visible = shouldCollapse ? allMessages.slice(-CHAT_COLLAPSE_VISIBLE_COUNT) : allMessages
  const hiddenCount = allMessages.length - visible.length

  async function handleSend(text: string, activityId?: string) {
    setSending(true)
    setPendingUserText(text)
    const result = await api.sendChatMessage(text, activityId)
    const assistantContent = result.ok ? result.reply : result.kind === "http" ? `Error: ${result.message}` : result.message
    qc.setQueryData<ChatMessage[]>(["chatHistory"], (prev) => [
      ...(prev ?? []),
      { role: "user", content: text, toolCalls: null, charts: null },
      {
        role: "assistant",
        content: assistantContent,
        toolCalls: result.ok ? result.toolCalls : null,
        charts: result.ok ? result.charts : null,
      },
    ])
    setPendingUserText(null)
    setSending(false)
  }

  async function handleReset() {
    await api.resetChat()
    qc.setQueryData<ChatMessage[]>(["chatHistory"], [])
    setHistoryExpanded(false)
  }

  // Consumes router state set by ActivityCard's "ask coach" button (ActivitiesPage.tsx) —
  // auto-fires the canned question once, then clears the state so back/forward
  // navigation doesn't replay it. Gated on `configured` so this never fires into the
  // "not configured" empty state. `prefillFired` guards against React 18 StrictMode's
  // dev-only double-invoke of this effect sending the message twice.
  useEffect(() => {
    if (prefillFired.current || !statusQuery.data?.configured) return
    const state = location.state as { prefillActivityId?: string; prefillText?: string } | null
    if (!state?.prefillText) return
    prefillFired.current = true
    void handleSend(state.prefillText, state.prefillActivityId)
    navigate(location.pathname, { replace: true, state: null })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fire once when configured status lands
  }, [statusQuery.data?.configured])

  // Pin to the newest message whenever the transcript changes. Runs on expand/collapse
  // too, since that also changes what's rendered. Jumps to the BOTTOM of the newest
  // message rather than its top — a long reply's start stays reachable by scrolling up,
  // which is the whole point of the mt-auto fix below.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [visible.length, pendingUserText, historyExpanded])

  const persona = personaQuery.data ? PERSONA_LABELS[personaQuery.data.personality] : null

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-bold">Coach</div>
        {historyQuery.data && historyQuery.data.length > 0 && (
          <button className="text-hale-faint text-xs hover:text-foreground" onClick={handleReset}>
            Clear conversation
          </button>
        )}
      </div>

      {!historyQuery.data || !statusQuery.data ? (
        <Skeleton className="h-64 w-full" />
      ) : (
        <>
          {/* `justify-end` on this scroller was a real bug: once the conversation
              overflowed, the content spilled past the TOP edge, and scrollTop can't go
              negative — so the start of a long reply became permanently unreachable.
              `mt-auto` on the inner wrapper keeps the same bottom-pinned look when
              content is short, while overflow grows downward into scrollable space. */}
          <div ref={scrollRef} className="flex flex-1 flex-col overflow-y-auto">
            <div className="mt-auto flex flex-col gap-2.5">
              {allMessages.length === 0 ? (
                <EmptyState
                  icon={MessageCircle}
                  title={persona ? `Chat with your ${persona.name}` : "Chat with your coach"}
                  message={persona?.blurb ?? "Ask about your training — grounded in your real data."}
                />
              ) : (
                <>
                  {allMessages.length > CHAT_COLLAPSE_VISIBLE_COUNT && (
                    <button
                      className="border-border text-hale-faint hover:text-foreground hover:border-muted-foreground self-center rounded-xl border px-2.5 py-1 text-[11px]"
                      onClick={() => setHistoryExpanded(!historyExpanded)}
                    >
                      {shouldCollapse ? `▲ Show earlier (${hiddenCount})` : "▼ Hide earlier"}
                    </button>
                  )}
                  {visible.map((m, i) => (
                    <ChatBubble key={i} msg={m} />
                  ))}
                </>
              )}
            </div>
          </div>

          {statusQuery.data.configured ? (
            <ChatInputBar sending={sending} onSend={handleSend} />
          ) : (
            <EmptyState
              icon={MessageCircle}
              title="AI assistant isn't configured yet"
              message="Add CLAUDE_CODE_OAUTH_TOKEN (Claude Pro/Max subscription) or ANTHROPIC_API_KEY to your .env and restart the container to enable chat — see .env.example for setup steps."
            />
          )}
        </>
      )}
    </div>
  )
}
