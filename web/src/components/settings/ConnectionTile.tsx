import type { ReactNode } from "react"

interface ConnectionTileProps {
  name: string
  icon: ReactNode
  connected: boolean
  onClick: () => void
}

export function ConnectionTile({ name, icon, connected, onClick }: ConnectionTileProps) {
  return (
    <button
      onClick={onClick}
      className={`flex flex-col items-center gap-2 rounded-lg p-4 transition-all ${
        connected
          ? "bg-hale-good/10 border border-hale-good/30 hover:bg-hale-good/20"
          : "bg-muted border border-border hover:bg-muted/80"
      }`}
    >
      <div className="text-3xl">{icon}</div>
      <div className="text-sm font-medium">{name}</div>
      <div className={`text-xs ${connected ? "text-hale-good" : "text-muted-foreground"}`}>
        {connected ? "Connected" : "Not connected"}
      </div>
    </button>
  )
}
