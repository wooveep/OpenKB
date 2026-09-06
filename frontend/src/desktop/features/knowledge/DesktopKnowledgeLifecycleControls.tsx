import { Archive, ArchiveRestore, CalendarClock, Trash2 } from "lucide-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { DesktopKnowledgeLifecycleState } from "@/desktop/bridge/contracts"

type Props = {
  pageId: string | undefined
  title: string
  lifecycleState: DesktopKnowledgeLifecycleState
  staleAfter: string | null
  isStale: boolean
  disabled: boolean
  onSetStaleAfter: (value: string | null) => Promise<void>
  onDeprecate: () => Promise<void>
  onRestore: () => Promise<void>
  onPermanentDelete: () => Promise<void>
}

/** Compact lifecycle controls keep destructive actions separate from editing. */
export function DesktopKnowledgeLifecycleControls({
  pageId,
  title,
  lifecycleState,
  staleAfter,
  isStale,
  disabled,
  onSetStaleAfter,
  onDeprecate,
  onRestore,
  onPermanentDelete,
}: Props) {
  const { t } = useTranslation("common")
  const [staleValue, setStaleValue] = useState(() => toLocalDateTime(staleAfter))

  if (!pageId || lifecycleState === "draft") return null

  const saveStaleAfter = async () => {
    await onSetStaleAfter(staleValue ? new Date(staleValue).toISOString() : null)
  }

  return (
    <div className="mt-4 flex flex-wrap items-end gap-3 rounded-xl border border-border/70 bg-muted/20 p-3">
      <div className="min-w-52 flex-1">
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
          {t("desktop.knowledgeBases.knowledgePages.lifecycle.title")}
        </p>
        <p className="mt-1 text-sm font-medium">
          {t(`desktop.knowledgeBases.knowledgePages.lifecycle.state.${lifecycleState}`)}
          {isStale ? ` · ${t("desktop.knowledgeBases.knowledgePages.lifecycle.stale")}` : ""}
        </p>
      </div>
      <label className="min-w-56 text-xs font-medium text-muted-foreground">
        {t("desktop.knowledgeBases.knowledgePages.lifecycle.staleAfter")}
        <Input
          className="mt-1"
          type="datetime-local"
          value={staleValue}
          disabled={disabled}
          onChange={(event) => setStaleValue(event.target.value)}
        />
      </label>
      <Button type="button" variant="outline" disabled={disabled} onClick={() => void saveStaleAfter()}>
        <CalendarClock className="size-4" />
        {staleValue
          ? t("desktop.knowledgeBases.knowledgePages.lifecycle.setStaleAfter")
          : t("desktop.knowledgeBases.knowledgePages.lifecycle.clearStaleAfter")}
      </Button>
      {lifecycleState === "stable" ? (
        <Button type="button" variant="outline" disabled={disabled} onClick={() => void onDeprecate()}>
          <Archive className="size-4" />
          {t("desktop.knowledgeBases.knowledgePages.lifecycle.deprecate")}
        </Button>
      ) : (
        <>
          <Button type="button" variant="outline" disabled={disabled} onClick={() => void onRestore()}>
            <ArchiveRestore className="size-4" />
            {t("desktop.knowledgeBases.knowledgePages.lifecycle.restore")}
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button type="button" variant="destructive" disabled={disabled}>
                <Trash2 className="size-4" />
                {t("desktop.knowledgeBases.knowledgePages.lifecycle.permanentDelete")}
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>
                  {t("desktop.knowledgeBases.knowledgePages.lifecycle.deleteTitle", { title })}
                </AlertDialogTitle>
                <AlertDialogDescription>
                  {t("desktop.knowledgeBases.knowledgePages.lifecycle.deleteDescription")}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>{t("actions.cancel")}</AlertDialogCancel>
                <AlertDialogAction
                  className="bg-destructive text-white hover:bg-destructive/90"
                  onClick={() => void onPermanentDelete()}
                >
                  {t("desktop.knowledgeBases.knowledgePages.lifecycle.confirmDelete")}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </>
      )}
    </div>
  )
}

function toLocalDateTime(value: string | null): string {
  if (!value) return ""
  const date = new Date(value)
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}
