import { convertFileSrc } from "@tauri-apps/api/core"
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

/** Read-only view of the verified original plus its independently retained images. */
export function DesktopRawDocumentDialog({
  document,
  focusLocator,
  loadingMore,
  onOpenChange,
  onLoadMore,
}: {
  document: DesktopRawDocument | null
  focusLocator: Record<string, unknown> | null
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
        {document && focusLocator ? (
          <p className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs text-foreground">
            {t("desktop.knowledgeBases.originalFocusLocation", {
              location: formatLocator(focusLocator),
            })}
          </p>
        ) : null}
        <pre className="min-h-0 flex-1 overflow-auto rounded-lg border border-border/70 bg-muted/30 p-4 font-mono2 text-xs leading-6 whitespace-pre-wrap">
          {document?.content}
        </pre>
        {document?.sourceImages.length ? (
          <section className="max-h-56 overflow-y-auto rounded-lg border border-border/70 p-3" aria-label="Source images">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {document.sourceImages.map((image) => {
                const source = image.filePath ? convertFileSrc(image.filePath) : ""
                return source ? (
                  <a
                    key={image.sourceImageId}
                    href={source}
                    target="_blank"
                    rel="noreferrer"
                    className="block overflow-hidden rounded-md border border-border/70 bg-muted/20"
                    title={image.altText ?? image.name}
                  >
                    <img
                      src={source}
                      alt={image.altText ?? image.name}
                      className="h-32 w-full object-contain"
                    />
                    <span className="block truncate border-t border-border/70 px-2 py-1 text-xs text-muted-foreground">
                      {image.altText ?? image.name}
                    </span>
                  </a>
                ) : null
              })}
            </div>
          </section>
        ) : null}
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

function formatLocator(locator: Record<string, unknown>): string {
  const values = [
    "page",
    "slide",
    "sheet",
    "cell_range",
    "cell",
    "line_start",
    "line_end",
    "paragraph",
    "table",
    "body_order",
    "ordinal",
  ].flatMap((key) => locator[key] === undefined ? [] : [`${key}: ${String(locator[key])}`])
  return values.length ? values.join(" · ") : "document"
}
