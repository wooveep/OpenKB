import { AlertTriangle, Loader2, RefreshCw, Server } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "./bridge-context"
import type { DesktopBridgeHandshake, DesktopEngineHealth } from "./contracts"

type EngineViewState =
  | { phase: "starting"; lastSequence?: number }
  | {
      phase: "ready"
      handshake: DesktopBridgeHandshake
      health: DesktopEngineHealth
      lastSequence?: number
    }
  | {
      phase: "unavailable"
      handshake: DesktopBridgeHandshake
      health: DesktopEngineHealth
      lastSequence?: number
    }
  | { phase: "error"; message: string; lastSequence?: number }

/** The first Desktop Workbench view: a real, retryable Engine health indicator. */
export default function DesktopStartup() {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [state, setState] = useState<EngineViewState>({ phase: "starting" })

  const refresh = useCallback(async () => {
    setState((current) => ({ phase: "starting", lastSequence: current.lastSequence }))
    try {
      const handshake = await bridge.handshake()
      const health = await bridge.health()
      setState((current) => {
        if (health.status === "starting") {
          return { phase: "starting", lastSequence: current.lastSequence }
        }
        return {
          phase: health.status,
          handshake,
          health,
          lastSequence: current.lastSequence,
        }
      })
    } catch (error) {
      setState((current) => ({
        phase: "error",
        message: error instanceof Error ? error.message : String(error),
        lastSequence: current.lastSequence,
      }))
    }
  }, [bridge])

  useEffect(() => {
    let active = true
    let unsubscribe: (() => void) | undefined
    void bridge
      .subscribe((event) => {
        if (active) setState((current) => ({ ...current, lastSequence: event.sequence }))
      })
      .then((dispose) => {
        if (active) unsubscribe = dispose
        else dispose()
      })
      .catch(() => undefined)
    void refresh()
    return () => {
      active = false
      unsubscribe?.()
    }
  }, [bridge, refresh])

  const icon =
    state.phase === "starting" ? (
      <Loader2 className="h-5 w-5 animate-spin text-accent-brand" />
    ) : state.phase === "ready" ? (
      <Server className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
    ) : (
      <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-400" />
    )

  return (
    <section
      aria-live="polite"
      data-state={state.phase}
      data-testid="desktop-engine-status"
      className="w-full max-w-2xl rounded-apple-lg border border-[hsl(var(--glass-border))] glass-2 p-6 shadow-glass"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 grid h-10 w-10 place-items-center rounded-xl bg-muted/60">{icon}</span>
        <div className="min-w-0 flex-1">
          <h1 className="text-lg font-semibold text-foreground">{t(`desktop.engine.${state.phase}.title`)}</h1>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            {state.phase === "error"
              ? t("desktop.engine.error.detail", { message: state.message })
              : t(`desktop.engine.${state.phase}.detail`)}
          </p>
          {state.phase !== "starting" && state.phase !== "error" ? (
            <div className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>
                {t("desktop.engine.version", { version: state.handshake.engineVersion })}
              </span>
              <span>
                {t("desktop.engine.protocol", { version: state.health.protocolVersion })}
              </span>
              {state.lastSequence !== undefined ? (
                <span>{t("desktop.engine.eventSequence", { sequence: state.lastSequence })}</span>
              ) : null}
            </div>
          ) : null}
          {state.phase === "unavailable" || state.phase === "error" ? (
            <Button className="mt-5" onClick={() => void refresh()}>
              <RefreshCw className="h-4 w-4" />
              {t("desktop.engine.retry")}
            </Button>
          ) : null}
        </div>
      </div>
    </section>
  )
}
