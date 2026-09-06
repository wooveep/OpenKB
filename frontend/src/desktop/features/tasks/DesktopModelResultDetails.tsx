import { useTranslation } from "react-i18next"

export interface DesktopModelResultDetailsValue {
  finishReason: string | null
  reasoningObserved: boolean | null
  finalContentObserved: boolean | null
  reasoningChunkCount: number | null
  finalChunkCount: number | null
  reasoningCharacterCount: number | null
  finalCharacterCount: number | null
  inputTokens: number | null
  outputTokens: number | null
  totalTokens: number | null
  providerRequestId: string | null
}

/** Expandable provider diagnostics that never include model or reasoning content. */
export function DesktopModelResultDetails({
  result,
}: {
  result: DesktopModelResultDetailsValue
}) {
  const { t } = useTranslation("common")
  const hasDetails = Object.values(result).some((value) => value !== null)
  if (!hasDetails) return null
  const rows = [
    ["finishReason", result.finishReason ?? "—"],
    ["reasoningObserved", result.reasoningObserved === null ? "—" : String(result.reasoningObserved)],
    ["finalContentObserved", result.finalContentObserved === null ? "—" : String(result.finalContentObserved)],
    ["reasoningChunks", result.reasoningChunkCount ?? "—"],
    ["finalChunks", result.finalChunkCount ?? "—"],
    ["reasoningCharacters", result.reasoningCharacterCount ?? "—"],
    ["finalCharacters", result.finalCharacterCount ?? "—"],
    ["tokens", result.totalTokens ?? "—"],
    ["providerRequestId", result.providerRequestId ?? "—"],
  ] as const
  return (
    <details className="mt-2 rounded-md border border-border/60 bg-background/60 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium">
        {t("desktop.knowledgeBases.modelResultDetails.title")}
      </summary>
      <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-muted-foreground">
        {rows.map(([label, value]) => (
          <div className="contents" key={label}>
            <dt>{t(`desktop.knowledgeBases.modelResultDetails.${label}`)}</dt>
            <dd className="break-all text-right font-mono">{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  )
}
