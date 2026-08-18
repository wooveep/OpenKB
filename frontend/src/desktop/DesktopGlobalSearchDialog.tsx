import { BookOpen, FileText, Loader2, MessageSquare } from "lucide-react"
import { useEffect, useState, type ComponentType } from "react"
import { useTranslation } from "react-i18next"
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { useDesktopBridge } from "./bridge-context"
import type {
  DesktopGlobalSearchResult,
  DesktopGlobalSearchResultKind,
} from "./contracts"

const groupOrder: DesktopGlobalSearchResultKind[] = ["document", "knowledge_page", "conversation"]

const icons: Record<DesktopGlobalSearchResultKind, ComponentType<{ className?: string }>> = {
  document: FileText,
  knowledge_page: BookOpen,
  conversation: MessageSquare,
}

/** Keyboard-first current-KB search across user-facing workspace content. */
export function DesktopGlobalSearchDialog({
  open,
  onOpenChange,
  onSelect,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (result: DesktopGlobalSearchResult) => void
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<DesktopGlobalSearchResult[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const changeOpen = (nextOpen: boolean) => {
    if (!nextOpen) {
      setQuery("")
      setResults([])
      setError(null)
      setLoading(false)
    }
    onOpenChange(nextOpen)
  }

  const changeQuery = (value: string) => {
    setQuery(value)
    if (!value.trim()) {
      setResults([])
      setError(null)
      setLoading(false)
    }
  }

  useEffect(() => {
    const normalized = query.trim()
    if (!open || !normalized) return
    let disposed = false
    const timeout = window.setTimeout(() => {
      setLoading(true)
      setError(null)
      void bridge.globalSearch(normalized)
        .then((payload) => {
          if (!disposed) setResults(payload.results)
        })
        .catch((reason) => {
          if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
        })
        .finally(() => {
          if (!disposed) setLoading(false)
        })
    }, 150)
    return () => {
      disposed = true
      window.clearTimeout(timeout)
    }
  }, [bridge, open, query])

  return (
    <CommandDialog
      open={open}
      onOpenChange={changeOpen}
      title={t("desktop.globalSearch.title")}
      description={t("desktop.globalSearch.description")}
      className="sm:max-w-2xl"
    >
      <CommandInput
        value={query}
        onValueChange={changeQuery}
        placeholder={t("desktop.globalSearch.placeholder")}
      />
      <CommandList className="max-h-[min(65vh,34rem)]">
        {loading ? <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />{t("desktop.globalSearch.searching")}</div> : null}
        {error ? <div className="px-4 py-6 text-sm text-destructive" role="alert">{error}</div> : null}
        {!loading && !error ? <CommandEmpty>{query.trim() ? t("desktop.globalSearch.empty") : t("desktop.globalSearch.hint")}</CommandEmpty> : null}
        {groupOrder.map((kind) => {
          const items = results.filter((result) => result.kind === kind)
          if (!items.length) return null
          return (
            <CommandGroup key={kind} heading={t(`desktop.globalSearch.groups.${kind}`)}>
              {items.map((result) => {
                const Icon = icons[result.kind]
                return (
                  <CommandItem
                    key={result.resultId}
                    value={`${result.kind} ${result.title} ${result.snippet}`}
                    onSelect={() => {
                      onSelect(result)
                      changeOpen(false)
                    }}
                    className="items-start py-3"
                  >
                    <Icon className="mt-0.5 size-4" />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2 font-medium">
                        <span className="truncate">{result.title}</span>
                        {result.status === "failed" ? <span className="rounded-full bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive">{t("desktop.globalSearch.unavailable")}</span> : null}
                      </span>
                      <span className="mt-0.5 line-clamp-2 block text-xs leading-5 text-muted-foreground">{result.snippet}</span>
                    </span>
                  </CommandItem>
                )
              })}
            </CommandGroup>
          )
        })}
      </CommandList>
    </CommandDialog>
  )
}
