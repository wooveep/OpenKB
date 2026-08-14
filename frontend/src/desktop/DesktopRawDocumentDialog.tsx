import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { DesktopRawDocument } from "./contracts"

/** Read-only view of the one integrity-checked original held in raw/. */
export function DesktopRawDocumentDialog({
  document,
  loadingMore,
  onOpenChange,
  onLoadMore,
}: {
  document: DesktopRawDocument | null
  loadingMore: boolean
  onOpenChange: (open: boolean) => void
  onLoadMore: () => void
}) {
  const { t } = useTranslation("common")
  return (
    <Dialog open={document !== null} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] max-w-4xl flex-col">
        <DialogHeader>
          <DialogTitle>{document?.name ?? t("desktop.knowledgeBases.originalTitle")}</DialogTitle>
          <DialogDescription>
            {document ? t("desktop.knowledgeBases.originalMetadata", {
              bytes: document.byteSize,
              sha256: document.assetSha256,
            }) : null}
          </DialogDescription>
        </DialogHeader>
        <pre className="min-h-0 flex-1 overflow-auto rounded-lg border border-border/70 bg-muted/30 p-4 font-mono2 text-xs leading-6 whitespace-pre-wrap">
          {document?.content}
        </pre>
        <DialogFooter>
          {document?.hasMore ? (
            <Button type="button" variant="outline" disabled={loadingMore} onClick={onLoadMore}>
              {t("desktop.knowledgeBases.loadMoreOriginal")}
            </Button>
          ) : null}
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            {t("actions.close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
