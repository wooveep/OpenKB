const sourceLocatorKeys = [
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
]

/** Format persisted source coordinates consistently across answer and reader views. */
export function formatSourceLocator(locator: Record<string, unknown>): string {
  const values = sourceLocatorKeys
    .flatMap((key) => locator[key] === undefined ? [] : [`${key}: ${String(locator[key])}`])
  return values.length ? values.join(" · ") : "document"
}
